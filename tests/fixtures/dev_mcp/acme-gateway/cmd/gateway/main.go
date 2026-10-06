package main

import (
	"log"
	"net/http"

	"example.com/acme/gateway/internal/config"
	"example.com/acme/gateway/internal/proxy"
)

type staticVerifier struct{}

func (staticVerifier) Verify(token string) (string, error) { return "user", nil }

func main() {
	cfg := config.Load()
	h, err := proxy.Handler(cfg, staticVerifier{})
	if err != nil {
		log.Fatal(err)
	}
	log.Fatal(http.ListenAndServe(cfg.Listen, h))
}
