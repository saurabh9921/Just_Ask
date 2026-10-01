# Plain HTTP locations: common headers + keep-alive to the upstream.
# Timeouts and proxy_http_version are set per server (overridable per location).
include /etc/nginx/snippets/proxy_headers.inc;
proxy_set_header Connection "";
