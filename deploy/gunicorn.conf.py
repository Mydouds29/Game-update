# Configuration gunicorn (chargée par game-update-web.service).
bind = "127.0.0.1:8000"          # jamais exposé directement : reverse proxy devant
workers = 2                      # suffisant pour un usage perso sur une petite instance
threads = 2
worker_class = "gthread"
timeout = 30
graceful_timeout = 20
max_requests = 1000              # recyclage périodique des workers
max_requests_jitter = 100
accesslog = None                 # l'application journalise déjà chaque requête (JSON)
errorlog = "-"
loglevel = "warning"
forwarded_allow_ips = "127.0.0.1"
limit_request_line = 4094
limit_request_fields = 50
