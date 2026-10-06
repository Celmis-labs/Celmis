// Package proxy forwards authenticated requests upstream.
package proxy

import (
	"net/http"
	"net/http/httputil"
	"net/url"

	"example.com/acme/gateway/internal/auth"
	"example.com/acme/gateway/internal/config"
)

// Handler returns the reverse proxy wrapped in the auth middleware.
func Handler(cfg config.Config, v auth.Verifier) (http.Handler, error) {
	target, err := url.Parse(cfg.Upstream)
	if err != nil {
		return nil, err
	}
	return auth.Middleware(cfg, v)(httputil.NewSingleHostReverseProxy(target)), nil
}
