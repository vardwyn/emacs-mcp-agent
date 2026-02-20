;;; emacs-mcp.el --- Emacs bridge for emacs-mcp -*- lexical-binding: t; -*-

(require 'cl-lib)
(require 'json)
(require 'subr-x)

(defgroup emacs-mcp nil
  "Human-in-the-loop MCP workflow for Emacs."
  :group 'tools
  :prefix "emacs-mcp-")

(defcustom emacs-mcp-project-root nil
  "Absolute project root used for path validation.
When nil, use `default-directory'."
  :type '(choice (const :tag "Use default-directory" nil) directory))

(defcustom emacs-mcp-submissions-buffer-name "*emacs-mcp submissions*"
  "Buffer name used for accumulated submissions."
  :type 'string)

(defcustom emacs-mcp-socket-file-name "emacs-mcp.sock"
  "Socket filename inside the emacs-mcp cache directory."
  :type 'string)

(defcustom emacs-mcp-feedback-schema-version 1
  "Schema version written to feedback inbox event files."
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
    ("emacs.append_submission" . emacs-mcp--rpc-append-submission))
  "Method dispatch table for incoming bridge RPC calls.")

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
  "Return canonical absolute project root."
  (file-truename
   (or emacs-mcp-project-root
       default-directory)))

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
  (emacs-mcp--todo 'emacs-mcp--atomic-write-json))

;; ---------------------------------------------------------------------------
;; Path validation module
;; ---------------------------------------------------------------------------

(defun emacs-mcp--path-remote-p (path)
  "Return non-nil if PATH is remote (TRAMP)."
  (file-remote-p path))

(defun emacs-mcp--validate-repo-relative-path (path)
  "Validate repo-relative PATH token."
  (emacs-mcp--todo 'emacs-mcp--validate-repo-relative-path))

(defun emacs-mcp--resolve-project-path (rel-path)
  "Resolve REL-PATH under project root and return canonical absolute path."
  (emacs-mcp--todo 'emacs-mcp--resolve-project-path))

(defun emacs-mcp--path-under-project-root-p (abs-path)
  "Return non-nil if ABS-PATH resolves inside project root."
  (emacs-mcp--todo 'emacs-mcp--path-under-project-root-p))

;; ---------------------------------------------------------------------------
;; Lifecycle module (public commands)
;; ---------------------------------------------------------------------------

(defun emacs-mcp-running-p ()
  "Return non-nil when local bridge server is running."
  (and (processp emacs-mcp--socket-process)
       (process-live-p emacs-mcp--socket-process)))

(defun emacs-mcp-start ()
  "Start emacs-mcp local socket server."
  (interactive)
  (emacs-mcp--start-socket-server)
  (emacs-mcp--install-kill-hook)
  (message "emacs-mcp started on %s" (emacs-mcp-socket-path)))

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

;; ---------------------------------------------------------------------------
;; Socket server / transport module
;; ---------------------------------------------------------------------------

(defun emacs-mcp--start-socket-server ()
  "Create local socket server process."
  (let ((socket-path (emacs-mcp-socket-path)))
    (when (emacs-mcp-running-p)
      (user-error "emacs-mcp socket server is already running"))
    (emacs-mcp--ensure-runtime-dirs)
    (when (file-exists-p socket-path)
      (if (emacs-mcp--socket-alive-p)
          (user-error "Socket already in use by another emacs-mcp instance: %s" socket-path)
        (emacs-mcp--server-log "Removing stale socket file: %s" socket-path)
        (emacs-mcp--cleanup-socket-file)))
    (setq emacs-mcp--socket-process
          (make-network-process
           :name "emacs-mcp-socket-server"
           :family 'local
           :service socket-path
           :server t
           :noquery t
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
  (ignore process chunk)
  nil)

(defun emacs-mcp--socket-sentinel (process event)
  "Socket sentinel for PROCESS with EVENT."
  (ignore event)
  (unless (eq process emacs-mcp--socket-process)
    (emacs-mcp--unregister-client process)))

(defun emacs-mcp--register-client (process)
  "Register PROCESS in `emacs-mcp--connections`."
  (puthash process (make-emacs-mcp-connection :process process :partial-input "") emacs-mcp--connections))

(defun emacs-mcp--unregister-client (process)
  "Unregister PROCESS from `emacs-mcp--connections`."
  (remhash process emacs-mcp--connections))

(defun emacs-mcp--send-response (process response)
  "Serialize RESPONSE and send to PROCESS."
  (ignore process response)
  (emacs-mcp--todo 'emacs-mcp--send-response))

;; ---------------------------------------------------------------------------
;; RPC module
;; ---------------------------------------------------------------------------

(defun emacs-mcp--decode-json-line (line)
  "Decode LINE JSON and return Lisp object."
  (ignore line)
  (emacs-mcp--todo 'emacs-mcp--decode-json-line))

(defun emacs-mcp--encode-json-line (object)
  "Encode OBJECT as compact one-line JSON."
  (ignore object)
  (emacs-mcp--todo 'emacs-mcp--encode-json-line))

(defun emacs-mcp--rpc-result (id result)
  "Build JSON-RPC-like success response with ID and RESULT."
  `((id . ,id) (result . ,result)))

(defun emacs-mcp--rpc-error (id code message)
  "Build JSON-RPC-like error response."
  `((id . ,id) (error . ((code . ,code) (message . ,message)))))

(defun emacs-mcp--parse-request (obj)
  "Validate and parse request OBJ into `emacs-mcp-request`."
  (ignore obj)
  (emacs-mcp--todo 'emacs-mcp--parse-request))

(defun emacs-mcp--dispatch-request (request)
  "Dispatch parsed REQUEST and return result or error object."
  (ignore request)
  (emacs-mcp--todo 'emacs-mcp--dispatch-request))

(defun emacs-mcp--rpc-handle-line (process line)
  "Parse and handle one incoming LINE from PROCESS."
  (ignore process line)
  (emacs-mcp--todo 'emacs-mcp--rpc-handle-line))

;; ---------------------------------------------------------------------------
;; RPC method handlers module
;; ---------------------------------------------------------------------------

(defun emacs-mcp--rpc-get-selection (params)
  "Handle `emacs.get_selection` with PARAMS."
  (ignore params)
  (emacs-mcp--todo 'emacs-mcp--rpc-get-selection))

(defun emacs-mcp--rpc-append-submission (params)
  "Handle `emacs.append_submission` with PARAMS."
  (ignore params)
  (emacs-mcp--todo 'emacs-mcp--rpc-append-submission))

;; ---------------------------------------------------------------------------
;; Selection module
;; ---------------------------------------------------------------------------

(defun emacs-mcp--selection-data ()
  "Return selection payload expected by `emacs.get_selection`."
  (emacs-mcp--todo 'emacs-mcp--selection-data))

(defun emacs-mcp--selection-point (pos)
  "Build point object for POS with line/col/pos."
  (ignore pos)
  (emacs-mcp--todo 'emacs-mcp--selection-point))

(defun emacs-mcp--selection-file-relative-path ()
  "Return repo-relative path for current buffer file."
  (emacs-mcp--todo 'emacs-mcp--selection-file-relative-path))

(defun emacs-mcp--selection-refused (reason)
  "Return `{ok:false}` payload with REASON."
  `((ok . :json-false) (reason . ,reason)))

;; ---------------------------------------------------------------------------
;; Submissions UI module
;; ---------------------------------------------------------------------------

(defun emacs-mcp-submissions-buffer ()
  "Return submissions buffer, creating it if needed."
  (get-buffer-create emacs-mcp-submissions-buffer-name))

(defun emacs-mcp-open-submissions ()
  "Open submissions buffer."
  (interactive)
  (emacs-mcp--todo 'emacs-mcp-open-submissions))

(defun emacs-mcp-open-review-layout ()
  "Open a review-focused 2-window layout."
  (interactive)
  (emacs-mcp--todo 'emacs-mcp-open-review-layout))

(defun emacs-mcp-open-target-at-point ()
  "Open/switch right pane to target file for submission at point."
  (interactive)
  (emacs-mcp--todo 'emacs-mcp-open-target-at-point))

(defun emacs-mcp--append-submission-section (path description diff)
  "Append one submission section for PATH, DESCRIPTION, and DIFF."
  (ignore path description diff)
  (emacs-mcp--todo 'emacs-mcp--append-submission-section))

(defun emacs-mcp--submission-path-at-point ()
  "Extract submission target path from current section."
  (emacs-mcp--todo 'emacs-mcp--submission-path-at-point))

(defun emacs-mcp--submission-section-heading (path timestamp)
  "Format submission heading for PATH and TIMESTAMP."
  (ignore path timestamp)
  (emacs-mcp--todo 'emacs-mcp--submission-section-heading))

;; ---------------------------------------------------------------------------
;; Feedback module
;; ---------------------------------------------------------------------------

(defun emacs-mcp-finalize-file (path &optional user-message)
  "Finalize active cycle for PATH with optional USER-MESSAGE."
  (interactive
   (list (read-file-name "File: " (emacs-mcp-project-root))
         (read-string "User message (optional): ")))
  (ignore path user-message)
  (emacs-mcp--todo 'emacs-mcp-finalize-file))

(defun emacs-mcp-finalize-current-buffer (&optional user-message)
  "Finalize current buffer file with optional USER-MESSAGE."
  (interactive (list (read-string "User message (optional): ")))
  (ignore user-message)
  (emacs-mcp--todo 'emacs-mcp-finalize-current-buffer))

(defun emacs-mcp--write-finalize-event (rel-path user-message)
  "Write one finalize event for REL-PATH and USER-MESSAGE."
  (ignore rel-path user-message)
  (emacs-mcp--todo 'emacs-mcp--write-finalize-event))

(defun emacs-mcp--finalize-event-file-path ()
  "Return path for a new finalize event file."
  (emacs-mcp--todo 'emacs-mcp--finalize-event-file-path))

(defun emacs-mcp--finalize-event-payload (rel-path user-message)
  "Build finalize event JSON payload."
  `((schema_version . ,emacs-mcp-feedback-schema-version)
    (path . ,rel-path)
    (user_message . ,(or user-message ""))))

(provide 'emacs-mcp)

;;; emacs-mcp.el ends here
