// Package config loads gateway settings from the environment.
package config

import "os"

// Config holds the gateway settings.
type Config struct {
	Listen    string
	Upstream  string
	JWTIssuer string
	// JWTKeyPath is the secret-store path the signing key is read from.
	JWTKeyPath string
	VaultAddr  string
}

// Load reads the settings from environment variables.
func Load() Config {
	return Config{
		Listen:     getenv("GATEWAY_LISTEN", ":8080"),
		Upstream:   os.Getenv("UPSTREAM_URL"),
		JWTIssuer:  getenv("JWT_ISSUER", "https://auth.example.com/realms/acme"),
		JWTKeyPath: getenv("JWT_KEY_PATH", "vault:secret/data/acme/gateway/jwt"),
		VaultAddr:  os.Getenv("VAULT_ADDR"),
	}
}

func getenv(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
