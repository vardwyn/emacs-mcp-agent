;;; emacs-mcp.el --- Emacs bridge for emacs-mcp -*- lexical-binding: t; -*-

(require 'cl-lib)
(require 'json)
(require 'subr-x)
(declare-function org-back-to-heading "org" (&optional invisible-ok))

(defgroup emacs-mcp nil
  "Human-in-the-loop MCP workflow for Emacs."
  :group 'tools
  :prefix "emacs-mcp-")

(defcustom emacs-mcp-project-root nil
  "Absolute project root used for path validation.
Set explicitly by `emacs-mcp-start' (interactive prompt or argument)."
  :type '(choice (const :tag "Unset" nil) directory))

(defcustom emacs-mcp-submissions-buffer-name "*emacs-mcp submissions*"
  "Buffer name used for accumulated submissions."
  :type 'string)

(defcustom emacs-mcp-socket-file-name "emacs-mcp.sock"
  "Socket filename inside the emacs-mcp cache directory."
  :type 'string)

(defcustom emacs-mcp-feedback-schema-version 1
  "Schema version written to feedback inbox event files."
  :type 'integer)

(defcustom emacs-mcp-max-request-line-bytes (* 1024 1024)
  "Maximum bytes for one newline-delimited JSON request line over the local socket.
When exceeded, the request is rejected and the client connection is closed."
  :type 'integer)

(defcustom emacs-mcp-max-selection-bytes (* 256 1024)
  "Maximum UTF-8 byte size for `emacs.get_selection` text payload.
When exceeded, selection returns `{ok:false, reason:\"refused\"}`."
  :type 'integer)

(cl-defstruct emacs-mcp-connection
  "State for one socket client connection."
  process
  partial-input)

(cl-defstruct emacs-mcp-request
  "Parsed JSON-RPC-like request."
  id
  method
  params)

(defvar emacs-mcp--socket-process nil
  "Server process listening on the emacs-mcp local socket.")

(defvar emacs-mcp--connections (make-hash-table :test #'eq)
  "Client process -> `emacs-mcp-connection'.")

(defvar emacs-mcp--kill-hook-installed nil
  "Non-nil when `kill-emacs-hook' cleanup was registered.")

(defvar emacs-mcp--rpc-method-handlers
  '(("emacs.get_selection" . emacs-mcp--rpc-get-selection)
    ("emacs.get_project_root" . emacs-mcp--rpc-get-project-root)
    ("emacs.append_submission" . emacs-mcp--rpc-append-submission))
  "Method dispatch table for incoming bridge RPC calls.")

(define-error 'emacs-mcp-json-parse-error "emacs-mcp JSON parse error")
(define-error 'emacs-mcp-invalid-request "emacs-mcp invalid request")
(define-error 'emacs-mcp-invalid-params "emacs-mcp invalid params")

;; ---------------------------------------------------------------------------
;; Generic scaffolding helpers
;; ---------------------------------------------------------------------------

(defun emacs-mcp--todo (symbol)
  "Raise a not-implemented error for SYMBOL."
  (error "emacs-mcp: %s is not implemented yet" symbol))

;; ---------------------------------------------------------------------------
;; Paths / filesystem module
;; ---------------------------------------------------------------------------

(defun emacs-mcp-project-root ()
  "Return canonical absolute project root.
Raise an error when root is not configured."
  (unless emacs-mcp-project-root
    (error "emacs-mcp project root is not set; run emacs-mcp-start with explicit root"))
  (emacs-mcp--canonicalize-project-root emacs-mcp-project-root))

(defun emacs-mcp--canonicalize-project-root (project-root)
  "Validate and canonicalize PROJECT-ROOT."
  (unless (and (stringp project-root) (not (string-empty-p project-root)))
    (error "Project root must be a non-empty string"))
  (let ((expanded (expand-file-name project-root)))
    (when (emacs-mcp--path-remote-p expanded)
      (error "Project root must be local, not remote"))
    (unless (file-directory-p expanded)
      (error "Project root directory does not exist: %s" expanded))
    (file-name-as-directory (file-truename expanded))))

(defun emacs-mcp-cache-dir ()
  "Return `${XDG_CACHE_HOME:-~/.cache}/emacs-mcp`."
  (expand-file-name
   "emacs-mcp"
   (or (getenv "XDG_CACHE_HOME")
       (expand-file-name "~/.cache"))))

(defun emacs-mcp-socket-path ()
  "Return bridge socket path."
  (expand-file-name emacs-mcp-socket-file-name (emacs-mcp-cache-dir)))

(defun emacs-mcp-feedback-dir ()
  "Return feedback base directory."
  (expand-file-name "feedback" (emacs-mcp-cache-dir)))

(defun emacs-mcp-feedback-inbox-dir ()
  "Return feedback inbox directory."
  (expand-file-name "inbox" (emacs-mcp-feedback-dir)))

(defun emacs-mcp--ensure-runtime-dirs ()
  "Create required directories with private permissions."
  (dolist (dir (list (emacs-mcp-cache-dir)
                     (emacs-mcp-feedback-dir)
                     (emacs-mcp-feedback-inbox-dir)))
    (make-directory dir t)
    (emacs-mcp--set-private-mode dir #o700)))

(defun emacs-mcp--set-private-mode (path mode)
  "Apply MODE to PATH (directory or file)."
  (condition-case err
      (set-file-modes path mode)
    (file-error
     (emacs-mcp--server-log "Failed to set mode %o on %s: %s" mode path err))))

(defun emacs-mcp--atomic-write-json (path payload)
  "Atomically write JSON PAYLOAD to PATH."
  (unless (and (stringp path) (not (string-empty-p path)))
    (error "Atomic write path must be a non-empty string"))
  (when (emacs-mcp--path-remote-p path)
    (error "Atomic write path must be local"))
  (let* ((target-path (expand-file-name path))
         (target-dir (file-name-directory target-path))
         (target-name (file-name-nondirectory target-path))
         (temp-path nil)
         (json-encoding-pretty-print nil)
         (json-false :json-false)
         (json-null nil)
         (payload-text (concat (json-encode payload) "\n")))
    (unless (and target-dir (not (string-empty-p target-dir)))
      (error "Atomic write target has no parent directory: %s" target-path))
    (make-directory target-dir t)
    (emacs-mcp--set-private-mode target-dir #o700)
    (unwind-protect
        (progn
          (setq temp-path
                (make-temp-file
                 (concat (file-name-as-directory target-dir)
                         "."
                         target-name
                         ".tmp-")))
          (let ((coding-system-for-write 'utf-8-unix))
            (with-temp-file temp-path
              (insert payload-text)))
          (emacs-mcp--set-private-mode temp-path #o600)
          (rename-file temp-path target-path t)
          (setq temp-path nil)
          (emacs-mcp--set-private-mode target-path #o600)
          target-path)
      (when (and temp-path (file-exists-p temp-path))
        (ignore-errors
          (delete-file temp-path))))))

;; ---------------------------------------------------------------------------
;; Path validation module
;; ---------------------------------------------------------------------------

(defun emacs-mcp--path-remote-p (path)
  "Return non-nil if PATH is remote (TRAMP)."
  (file-remote-p path))

(defun emacs-mcp--validate-repo-relative-path (path)
  "Validate repo-relative PATH token."
  (unless (and (stringp path) (not (string-empty-p path)))
    (error "Path must be a non-empty string"))
  (when (string-match-p "\0" path)
    (error "Path must not contain NUL bytes"))
  (when (file-name-absolute-p path)
    (error "Path must be repo-relative"))
  (when (member ".." (split-string path "/" t))
    (error "Path traversal is not allowed"))
  path)

(defun emacs-mcp--resolve-project-path (rel-path)
  "Resolve REL-PATH under project root and return canonical absolute path."
  (emacs-mcp--validate-repo-relative-path rel-path)
  (let* ((root (file-name-as-directory (emacs-mcp-project-root)))
         (candidate (file-truename (expand-file-name rel-path root))))
    (unless (emacs-mcp--path-under-project-root-p candidate)
      (error "Path escapes project root"))
    candidate))

(defun emacs-mcp--path-under-project-root-p (abs-path)
  "Return non-nil if ABS-PATH resolves inside project root."
  (let ((root (file-name-as-directory (file-truename (emacs-mcp-project-root))))
        (candidate (file-truename abs-path)))
    (and (not (emacs-mcp--path-remote-p candidate))
         (file-in-directory-p candidate root))))

;; ---------------------------------------------------------------------------
;; Lifecycle module (public commands)
;; ---------------------------------------------------------------------------

(defun emacs-mcp-running-p ()
  "Return non-nil when local bridge server is running."
  (and (processp emacs-mcp--socket-process)
       (process-live-p emacs-mcp--socket-process)))

(defun emacs-mcp-start (&optional project-root)
  "Start emacs-mcp local socket server with explicit PROJECT-ROOT."
  (interactive
   (list (read-directory-name "Project root: " nil nil t)))
  (unless project-root
    (user-error "Project root is required"))
  (let ((canonical-root (emacs-mcp--canonicalize-project-root project-root))
        (previous-root emacs-mcp-project-root))
    (emacs-mcp--ensure-socket-server-startable)
    (setq emacs-mcp-project-root canonical-root)
    (condition-case err
        (progn
          (emacs-mcp--start-socket-server t)
          (emacs-mcp--install-kill-hook)
          (message "emacs-mcp started on %s (root: %s)"
                   (emacs-mcp-socket-path)
                   (emacs-mcp-project-root)))
      (error
       (setq emacs-mcp-project-root previous-root)
       (signal (car err) (cdr err))))))

(defun emacs-mcp-stop ()
  "Stop emacs-mcp local socket server and cleanup socket path."
  (interactive)
  (emacs-mcp--stop-socket-server)
  (emacs-mcp--remove-kill-hook)
  (emacs-mcp--cleanup-socket-file)
  (message "emacs-mcp stopped"))

(defun emacs-mcp--cleanup-socket-file ()
  "Best-effort cleanup of socket file."
  (let ((socket-path (emacs-mcp-socket-path)))
    (when (file-exists-p socket-path)
      (condition-case err
          (delete-file socket-path)
        (file-error
         (emacs-mcp--server-log "Failed to delete socket file %s: %s" socket-path err))))))

(defun emacs-mcp--socket-alive-p ()
  "Return non-nil if socket path exists and accepts a connection."
  (let ((socket-path (emacs-mcp-socket-path))
        (probe nil)
        (alive nil))
    (when (file-exists-p socket-path)
      (setq alive
            (condition-case nil
                (progn
                  (setq probe (make-network-process
                               :name "emacs-mcp-probe"
                               :family 'local
                               :service socket-path
                               :noquery t
                               :nowait nil))
                  t)
              (file-error nil)
              (error nil)))
      (when (processp probe)
        (delete-process probe)))
    alive))

(defun emacs-mcp--install-kill-hook ()
  "Install kill hook for best-effort socket cleanup."
  (unless emacs-mcp--kill-hook-installed
    (add-hook 'kill-emacs-hook #'emacs-mcp--cleanup-socket-file)
    (setq emacs-mcp--kill-hook-installed t)))

(defun emacs-mcp--remove-kill-hook ()
  "Remove kill hook installed by emacs-mcp."
  (when emacs-mcp--kill-hook-installed
    (remove-hook 'kill-emacs-hook #'emacs-mcp--cleanup-socket-file)
    (setq emacs-mcp--kill-hook-installed nil)))

(defun emacs-mcp--ensure-socket-server-startable ()
  "Ensure socket path is ready for starting a local bridge server."
  (let ((socket-path (emacs-mcp-socket-path)))
    (when (emacs-mcp-running-p)
      (user-error "emacs-mcp socket server is already running"))
    (emacs-mcp--ensure-runtime-dirs)
    (when (file-exists-p socket-path)
      (if (emacs-mcp--socket-alive-p)
          (user-error "Socket already in use by another emacs-mcp instance: %s" socket-path)
        (emacs-mcp--server-log "Removing stale socket file: %s" socket-path)
        (emacs-mcp--cleanup-socket-file)))
    socket-path))

;; ---------------------------------------------------------------------------
;; Socket server / transport module
;; ---------------------------------------------------------------------------

(defun emacs-mcp--start-socket-server (&optional prechecked)
  "Create local socket server process.
When PRECHECKED is non-nil, startup preconditions are assumed to be satisfied."
  (let ((socket-path
         (if prechecked
             (emacs-mcp-socket-path)
           (emacs-mcp--ensure-socket-server-startable))))
    (setq emacs-mcp--socket-process
          (make-network-process
           :name "emacs-mcp-socket-server"
           :family 'local
           :service socket-path
           :server t
           :noquery t
           :log #'emacs-mcp--socket-log
           :filter #'emacs-mcp--socket-filter
           :sentinel #'emacs-mcp--socket-sentinel))
    (emacs-mcp--set-private-mode socket-path #o600)
    emacs-mcp--socket-process))

(defun emacs-mcp--stop-socket-server ()
  "Stop local socket server process and all clients."
  (maphash (lambda (proc _conn)
             (when (process-live-p proc)
               (delete-process proc)))
           emacs-mcp--connections)
  (clrhash emacs-mcp--connections)
  (when (emacs-mcp-running-p)
    (delete-process emacs-mcp--socket-process))
  (setq emacs-mcp--socket-process nil))

(defun emacs-mcp--server-log (fmt &rest args)
  "Write diagnostics for the bridge server."
  (apply #'message (concat "emacs-mcp: " fmt) args))

(defun emacs-mcp--socket-filter (process chunk)
  "Socket filter for incoming CHUNK from PROCESS."
  (if-let* ((conn (gethash process emacs-mcp--connections)))
      (let ((partial (or (emacs-mcp-connection-partial-input conn) ""))
            (start 0)
            newline-index
            (drop-client nil))
        (while (and (not drop-client)
                    (setq newline-index (string-match "\n" chunk start)))
          (let* ((segment (substring chunk start newline-index))
                 (line-bytes (+ (string-bytes partial) (string-bytes segment))))
            (when (> line-bytes emacs-mcp-max-request-line-bytes)
              (emacs-mcp--server-log "Dropping client: oversized request line")
              (emacs-mcp--send-response
               process
               (emacs-mcp--rpc-error nil "invalid_request" "Request line exceeds size limit"))
              (setq drop-client t))
            (unless drop-client
              (let ((line (if (string-empty-p partial)
                              segment
                            (concat partial segment))))
                (setq partial "")
                (condition-case err
                    (emacs-mcp--rpc-handle-line process line)
                  (error
                   (emacs-mcp--server-log "Failed to handle socket line: %s" err))))))
          (setq start (1+ newline-index)))
        (unless drop-client
          (let* ((tail (substring chunk start))
                 (tail-bytes (+ (string-bytes partial) (string-bytes tail))))
            (if (> tail-bytes emacs-mcp-max-request-line-bytes)
                (progn
                  (emacs-mcp--server-log "Dropping client: partial line exceeds size limit")
                  (emacs-mcp--send-response
                   process
                   (emacs-mcp--rpc-error nil "invalid_request" "Request line exceeds size limit"))
                  (setq drop-client t))
              (setq partial
                    (if (string-empty-p partial)
                        tail
                      (concat partial tail))))))
        (if drop-client
            (emacs-mcp--close-client process)
          (setf (emacs-mcp-connection-partial-input conn) partial)))
    (emacs-mcp--server-log "Dropping untracked client in socket filter")
    (emacs-mcp--close-client process)))

(defun emacs-mcp--socket-sentinel (process event)
  "Socket sentinel for PROCESS with EVENT."
  (ignore event)
  (unless (eq process emacs-mcp--socket-process)
    (emacs-mcp--unregister-client process)))

(defun emacs-mcp--socket-log (server client message)
  "Track socket CLIENT lifecycle events for SERVER."
  (ignore server message)
  (if (process-live-p client)
      (emacs-mcp--register-client client)
    (emacs-mcp--unregister-client client)))

(defun emacs-mcp--register-client (process)
  "Register PROCESS in `emacs-mcp--connections`."
  (puthash process (make-emacs-mcp-connection :process process :partial-input "") emacs-mcp--connections))

(defun emacs-mcp--unregister-client (process)
  "Unregister PROCESS from `emacs-mcp--connections`."
  (remhash process emacs-mcp--connections))

(defun emacs-mcp--close-client (process)
  "Close PROCESS and remove it from connection table."
  (emacs-mcp--unregister-client process)
  (when (process-live-p process)
    (delete-process process)))

(defun emacs-mcp--send-response (process response)
  "Serialize RESPONSE and send to PROCESS."
  (when (process-live-p process)
    (condition-case err
        (process-send-string process (emacs-mcp--encode-json-line response))
      (error
       (emacs-mcp--server-log "Failed to send response: %s" err)))))

;; ---------------------------------------------------------------------------
;; RPC module
;; ---------------------------------------------------------------------------

(defun emacs-mcp--decode-json-line (line)
  "Decode LINE JSON and return Lisp object."
  (condition-case err
      (let ((json-object-type 'alist)
            (json-array-type 'vector)
            (json-key-type 'string)
            (json-false :json-false)
            (json-null :json-null))
        (json-read-from-string line))
    (json-error
     (signal 'emacs-mcp-json-parse-error (list (error-message-string err))))))

(defun emacs-mcp--encode-json-line (object)
  "Encode OBJECT as compact one-line JSON."
  (let ((json-encoding-pretty-print nil)
        (json-false :json-false)
        (json-null nil))
    (concat (json-encode object) "\n")))

(defun emacs-mcp--rpc-result (id result)
  "Build JSON-RPC-like success response with ID and RESULT."
  `((id . ,id) (result . ,result)))

(defun emacs-mcp--rpc-error (id code message)
  "Build JSON-RPC-like error response."
  `((id . ,id) (error . ((code . ,code) (message . ,message)))))

(defun emacs-mcp--parse-request (obj)
  "Validate and parse request OBJ into `emacs-mcp-request`."
  (let ((missing (make-symbol "missing")))
    (unless (and (listp obj) (cl-every #'consp obj))
      (signal 'emacs-mcp-invalid-request '("Request must be a JSON object")))
    (let* ((id (alist-get "id" obj missing nil #'equal))
           (method (alist-get "method" obj missing nil #'equal))
           (jsonrpc (alist-get "jsonrpc" obj missing nil #'equal))
           (params-cell (assoc "params" obj))
           (params (if params-cell (cdr params-cell) '())))
      (unless (integerp id)
        (signal 'emacs-mcp-invalid-request '("Request 'id' must be an integer")))
      (unless (stringp method)
        (signal 'emacs-mcp-invalid-request '("Request 'method' must be a string")))
      (unless (or (eq jsonrpc missing) (equal jsonrpc "2.0"))
        (signal 'emacs-mcp-invalid-request '("Request 'jsonrpc' must be \"2.0\" when present")))
      (unless (and (listp params) (cl-every #'consp params))
        (signal 'emacs-mcp-invalid-request '("Request 'params' must be a JSON object when present")))
      (make-emacs-mcp-request :id id :method method :params params))))

(defun emacs-mcp--dispatch-request (request)
  "Dispatch parsed REQUEST and return result or error object."
  (let* ((request-id (emacs-mcp-request-id request))
         (method (emacs-mcp-request-method request))
         (handler (cdr (assoc method emacs-mcp--rpc-method-handlers))))
    (if (not handler)
        (emacs-mcp--rpc-error request-id "method_not_found" (format "Unknown method: %s" method))
      (condition-case err
          (let ((validated-params
                 (emacs-mcp--validate-method-params
                  method
                  (emacs-mcp-request-params request))))
            (emacs-mcp--rpc-result request-id (funcall handler validated-params)))
        (emacs-mcp-invalid-params
         (emacs-mcp--rpc-error request-id "invalid_params" (or (cadr err) "Invalid params")))
        (error
         (emacs-mcp--rpc-error request-id "internal_error" (error-message-string err)))))))

(defun emacs-mcp--validate-method-params (method params)
  "Validate PARAMS for METHOD and return normalized params."
  (pcase method
    ("emacs.get_selection"
     (when params
       (signal 'emacs-mcp-invalid-params '("Invalid params for emacs.get_selection: expected empty object")))
     '())
    ("emacs.get_project_root"
     (when params
       (signal 'emacs-mcp-invalid-params '("Invalid params for emacs.get_project_root: expected empty object")))
     '())
    ("emacs.append_submission"
     (unless (and (listp params) (cl-every #'consp params))
       (signal 'emacs-mcp-invalid-params
               '("Invalid params for emacs.append_submission: expected object")))
     (let* ((allowed-keys '("path" "description" "diff"))
            (unknown-keys
             (delete-dups
              (cl-loop for (key . _value) in params
                       unless (member key allowed-keys)
                       collect key)))
            (path (alist-get "path" params nil nil #'equal))
            (description (alist-get "description" params nil nil #'equal))
            (diff (alist-get "diff" params nil nil #'equal)))
       (when unknown-keys
         (signal 'emacs-mcp-invalid-params
                 (list
                  (format
                   "Invalid params for emacs.append_submission: unexpected keys: %s"
                   (string-join (sort unknown-keys #'string<) ", ")))))
       (unless (and (stringp path) (not (string-empty-p path)))
         (signal 'emacs-mcp-invalid-params
                 '("Invalid params for emacs.append_submission: 'path' must be a non-empty string")))
       (unless (stringp description)
         (signal 'emacs-mcp-invalid-params
                 '("Invalid params for emacs.append_submission: 'description' must be a string")))
       (unless (stringp diff)
         (signal 'emacs-mcp-invalid-params
                 '("Invalid params for emacs.append_submission: 'diff' must be a string")))
       (condition-case err
           (emacs-mcp--validate-repo-relative-path path)
         (error
          (signal 'emacs-mcp-invalid-params
                  (list
                   (format
                    "Invalid params for emacs.append_submission: invalid 'path': %s"
                    (error-message-string err))))))
       `((path . ,path) (description . ,description) (diff . ,diff))))
    (_ (error "Missing params validator for method: %s" method))))

(defun emacs-mcp--extract-request-id (obj)
  "Extract integer request ID from decoded JSON OBJ, or nil."
  (when (and (listp obj) (cl-every #'consp obj))
    (let ((id (alist-get "id" obj nil nil #'equal)))
      (when (integerp id)
        id))))

(defun emacs-mcp--rpc-handle-line (process line)
  "Parse and handle one incoming LINE from PROCESS."
  (let ((decoded nil)
        (request-id nil)
        (response nil))
    (condition-case err
        (progn
          (setq decoded (emacs-mcp--decode-json-line line))
          (setq request-id (emacs-mcp--extract-request-id decoded))
          (setq response (emacs-mcp--dispatch-request (emacs-mcp--parse-request decoded))))
      (emacs-mcp-json-parse-error
       (setq response (emacs-mcp--rpc-error nil "parse_error" (car (cdr err)))))
      (emacs-mcp-invalid-request
       (setq response (emacs-mcp--rpc-error request-id "invalid_request" (car (cdr err)))))
      (emacs-mcp-invalid-params
       (setq response (emacs-mcp--rpc-error request-id "invalid_params" (car (cdr err)))))
      (error
       (setq response (emacs-mcp--rpc-error request-id "internal_error" (error-message-string err)))))
    (emacs-mcp--send-response process response)))

;; ---------------------------------------------------------------------------
;; RPC method handlers module
;; ---------------------------------------------------------------------------

(defun emacs-mcp--rpc-get-selection (params)
  "Handle `emacs.get_selection` with PARAMS."
  (ignore params)
  (emacs-mcp--selection-data))

(defun emacs-mcp--rpc-get-project-root (params)
  "Handle `emacs.get_project_root` with PARAMS."
  (ignore params)
  `((ok . t) (project_root . ,(emacs-mcp-project-root))))

(defun emacs-mcp--rpc-append-submission (params)
  "Handle `emacs.append_submission` with PARAMS."
  (let ((path (alist-get 'path params))
        (description (alist-get 'description params))
        (diff (alist-get 'diff params)))
    (emacs-mcp--append-submission-section path description diff)
    '((ok . t))))

;; ---------------------------------------------------------------------------
;; Selection module
;; ---------------------------------------------------------------------------

(defun emacs-mcp--selection-data ()
  "Return selection payload expected by `emacs.get_selection`."
  (if (not (use-region-p))
      (emacs-mcp--selection-refused "no active region")
    (let ((rel-path (emacs-mcp--selection-file-relative-path)))
      (if (not rel-path)
          (emacs-mcp--selection-refused "outside project root")
        (let* ((start (region-beginning))
               (end (region-end))
               (text (buffer-substring-no-properties start end)))
          (if (> (string-bytes text) emacs-mcp-max-selection-bytes)
              (emacs-mcp--selection-refused "refused")
            `((ok . t)
              (file . ,rel-path)
              (start . ,(emacs-mcp--selection-point start))
              (end . ,(emacs-mcp--selection-point end))
              (text . ,text))))))))

(defun emacs-mcp--selection-point (pos)
  "Build point object for POS with line/col/pos."
  (save-excursion
    (goto-char pos)
    `((line . ,(line-number-at-pos pos t))
      (col . ,(current-column))
      (pos . ,pos))))

(defun emacs-mcp--selection-file-relative-path ()
  "Return repo-relative path for current buffer file."
  (let ((path (buffer-file-name)))
    (when (and path (not (emacs-mcp--path-remote-p path)))
      (let ((abs-path (file-truename path))
            (root (file-truename (emacs-mcp-project-root))))
        (when (emacs-mcp--path-under-project-root-p abs-path)
          (file-relative-name abs-path (file-name-as-directory root)))))))

(defun emacs-mcp--selection-refused (reason)
  "Return `{ok:false}` payload with REASON."
  `((ok . :json-false) (reason . ,reason)))

;; ---------------------------------------------------------------------------
;; Submissions UI module
;; ---------------------------------------------------------------------------

(defun emacs-mcp-submissions-buffer ()
  "Return submissions buffer, creating it if needed."
  (let ((buffer (get-buffer-create emacs-mcp-submissions-buffer-name)))
    (with-current-buffer buffer
      (unless (derived-mode-p 'org-mode)
        (org-mode))
      (setq-local truncate-lines t))
    buffer))

(defun emacs-mcp-open-submissions ()
  "Open submissions buffer."
  (interactive)
  (let ((buffer (emacs-mcp-submissions-buffer)))
    (pop-to-buffer buffer)
    (goto-char (point-max))
    buffer))

(defun emacs-mcp-open-review-layout ()
  "Open a review-focused 2-window layout."
  (interactive)
  (let* ((submissions-buffer (emacs-mcp-submissions-buffer))
         (project-root (emacs-mcp-project-root))
         (current-buffer-file (buffer-file-name))
         (target-buffer
          (when (and current-buffer-file
                     (not (emacs-mcp--path-remote-p current-buffer-file))
                     (emacs-mcp--path-under-project-root-p (file-truename current-buffer-file)))
            (current-buffer))))
    (delete-other-windows)
    (let* ((left-window (selected-window))
           (right-window (split-window-right)))
      (set-window-buffer left-window submissions-buffer)
      (with-selected-window left-window
        (goto-char (point-max)))
      (if target-buffer
          (set-window-buffer right-window target-buffer)
        (with-selected-window right-window
          (dired project-root)))
      (select-window left-window))))

(defun emacs-mcp-open-target-at-point ()
  "Open/switch right pane to target file for submission at point."
  (interactive)
  (let* ((rel-path (emacs-mcp--submission-path-at-point))
         (abs-path (emacs-mcp--resolve-project-path rel-path))
         (target-buffer (find-file-noselect abs-path))
         (target-window
          (if (= (count-windows) 1)
              (split-window-right)
            (or (window-in-direction 'right)
                (next-window)))))
    (set-window-buffer target-window target-buffer)
    (message "emacs-mcp opened target: %s" rel-path)))

(defun emacs-mcp--append-submission-section (path description diff)
  "Append one submission section for PATH, DESCRIPTION, and DIFF."
  (unless (stringp path)
    (error "Submission path must be a string"))
  (unless (stringp description)
    (error "Submission description must be a string"))
  (unless (stringp diff)
    (error "Submission diff must be a string"))
  (emacs-mcp--validate-repo-relative-path path)
  (let* ((timestamp (format-time-string "%Y-%m-%d %H:%M:%S"))
         (heading (emacs-mcp--submission-section-heading path timestamp))
         (description-text
          (let ((trimmed (string-trim-right description)))
            (if (string-empty-p trimmed)
                "(no description)"
              trimmed)))
         (diff-text
          (if (string-suffix-p "\n" diff)
              diff
            (concat diff "\n")))
         (section
          (concat
           heading "\n\n"
           description-text "\n\n"
           "#+begin_src diff\n"
           diff-text
           "#+end_src\n")))
    (with-current-buffer (emacs-mcp-submissions-buffer)
      (goto-char (point-max))
      (unless (bobp)
        (unless (eq (char-before) ?\n)
          (insert "\n"))
        (insert "\n"))
      (insert section)
      (goto-char (point-max))
      t)))

(defun emacs-mcp--submission-path-at-point ()
  "Extract submission target path from current section."
  (let* ((raw-line
          (save-excursion
            (if (derived-mode-p 'org-mode)
                (progn
                  (unless (fboundp 'org-back-to-heading)
                    (require 'org))
                  (org-back-to-heading t)
                  (buffer-substring-no-properties
                   (line-beginning-position)
                   (line-end-position)))
              (or (thing-at-point 'line t) ""))))
         (line (string-trim raw-line))
         (path
          (cond
           ((string-match
             "^\\*+\\s-+\\(.+?\\)\\(?:\\s-+\\[[^]]+\\]\\)?\\s-*$"
             line)
            (match-string 1 line))
           ((string-match "path:\\s-*\\(.+\\)$" line)
            (match-string 1 line))
           (t nil))))
    (unless path
      (user-error "Cannot extract submission path at point"))
    (condition-case err
        (progn
          (emacs-mcp--validate-repo-relative-path path)
          path)
      (error
       (user-error "Invalid submission path at point: %s"
                   (error-message-string err))))))

(defun emacs-mcp--submission-section-heading (path timestamp)
  "Format submission heading for PATH and TIMESTAMP."
  (unless (and (stringp path) (not (string-empty-p path)))
    (error "Submission heading path must be a non-empty string"))
  (unless (and (stringp timestamp) (not (string-empty-p timestamp)))
    (error "Submission heading timestamp must be a non-empty string"))
  (format "* %s [%s]" path timestamp))

;; ---------------------------------------------------------------------------
;; Feedback module
;; ---------------------------------------------------------------------------

(defun emacs-mcp--normalize-finalize-path (path)
  "Normalize PATH and return repo-relative path under project root."
  (unless (and (stringp path) (not (string-empty-p path)))
    (user-error "File path is required"))
  (when (emacs-mcp--path-remote-p path)
    (user-error "Cannot finalize remote path: %s" path))
  (let* ((project-root (file-name-as-directory (emacs-mcp-project-root)))
         (candidate-path
          (if (file-name-absolute-p path)
              path
            (expand-file-name path project-root))))
    (condition-case err
        (let ((resolved-path (file-truename candidate-path)))
          (unless (emacs-mcp--path-under-project-root-p resolved-path)
            (user-error "File is outside project root: %s" candidate-path))
          (when (and (file-exists-p resolved-path)
                     (not (file-regular-p resolved-path)))
            (user-error "Finalize target is not a regular file: %s" candidate-path))
          (let ((rel-path (file-relative-name resolved-path project-root)))
            (emacs-mcp--validate-repo-relative-path rel-path)
            rel-path))
      (error
       (user-error "Invalid finalize path: %s" (error-message-string err))))))

(defun emacs-mcp-finalize-file (path &optional user-message)
  "Finalize active cycle for PATH with optional USER-MESSAGE."
  (interactive
   (list (read-file-name "File: " (emacs-mcp-project-root))
         (read-string "User message (optional): ")))
  (unless (or (null user-message) (stringp user-message))
    (user-error "User message must be a string"))
  (let* ((rel-path (emacs-mcp--normalize-finalize-path path))
         (event-path (emacs-mcp--write-finalize-event rel-path user-message)))
    (message "emacs-mcp queued finalize event for %s" rel-path)
    event-path))

(defun emacs-mcp-finalize-current-buffer (&optional user-message)
  "Finalize current buffer file with optional USER-MESSAGE."
  (interactive (list (read-string "User message (optional): ")))
  (let ((path (buffer-file-name)))
    (unless path
      (user-error "Current buffer is not visiting a file"))
    (emacs-mcp-finalize-file path user-message)))

(defun emacs-mcp--write-finalize-event (rel-path user-message)
  "Write one finalize event for REL-PATH and USER-MESSAGE."
  (emacs-mcp--validate-repo-relative-path rel-path)
  (unless (or (null user-message) (stringp user-message))
    (error "Finalize user message must be a string or nil"))
  (emacs-mcp--ensure-runtime-dirs)
  (let* ((payload (emacs-mcp--finalize-event-payload rel-path user-message))
         (event-path (emacs-mcp--finalize-event-file-path)))
    (emacs-mcp--atomic-write-json event-path payload)
    event-path))

(defun emacs-mcp--finalize-event-file-path ()
  "Return path for a new finalize event file."
  (let* ((inbox-dir (emacs-mcp-feedback-inbox-dir))
         (timestamp (format-time-string "%Y%m%dT%H%M%S%6NZ" (current-time) t))
         (pid (or (emacs-pid) 0))
         (random-fragment (format "%06x" (random #x1000000)))
         (filename (format "event-%s-%d-%s.json" timestamp pid random-fragment)))
    (expand-file-name filename inbox-dir)))

(defun emacs-mcp--finalize-event-payload (rel-path user-message)
  "Build finalize event JSON payload."
  `((schema_version . ,emacs-mcp-feedback-schema-version)
    (path . ,rel-path)
    (user_message . ,(or user-message ""))))

(provide 'emacs-mcp)

;;; emacs-mcp.el ends here
