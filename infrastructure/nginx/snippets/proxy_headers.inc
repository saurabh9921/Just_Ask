# Request headers for every proxied location (WebSocket locations add Upgrade/Connection).
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $remote_addr;   # overwrite, never append client-supplied chains
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Request-ID $req_id;
