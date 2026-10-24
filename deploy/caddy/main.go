// Standard upstream Caddy entry point; no third-party plugins are added.
// Supported build pattern: github.com/caddyserver/caddy/v2/cmd/caddy/main.go.
package main

import (
	_ "time/tzdata"

	caddycmd "github.com/caddyserver/caddy/v2/cmd"
	_ "github.com/caddyserver/caddy/v2/modules/standard"
)

func main() {
	caddycmd.Main()
}
