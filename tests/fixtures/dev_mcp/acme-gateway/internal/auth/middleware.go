// Package auth verifies bearer tokens on every request.
package auth

import (
	"net/http"
	"strings"

	"example.com/acme/gateway/internal/config"
)

// Verifier checks a token and returns the subject.
type Verifier interface {
	Verify(token string) (subject string, err error)
}

// Middleware rejects requests without a valid bearer token.
func Middleware(cfg config.Config, v Verifier) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			header := r.Header.Get("Authorization")
			token := strings.TrimPrefix(header, "Bearer ")
			if token == "" || token == header {
				http.Error(w, "missing bearer token", http.StatusUnauthorized)
				return
			}
			if _, err := v.Verify(token); err != nil {
				http.Error(w, "invalid token", http.StatusUnauthorized)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}
