# Extra trusted root certificates

Networks that inspect TLS (for example Cato Networks or Zscaler) replace public certificates with ones signed by
their own root CA. Docker builds then fail to download npm and Python packages, and discovery can't reach Microsoft
Graph, because that root isn't in the image's trust store.

Put the network's root CA here as a **PEM file ending in `.crt`** (text starting with
`-----BEGIN CERTIFICATE-----`). The image build adds every `ca-certs/*.crt` to the system trust store before it
downloads anything (npm through `NODE_EXTRA_CA_CERTS`, Python and uv through the system bundle), and the app trusts it
at runtime. `scripts/setup.ps1` can export it from the Windows certificate store for you when it detects
interception; `scripts/setup.sh` tells you when one is needed.

Only add a root certificate your IT team distributes. The `.crt` and `.pem` files are not committed (`.gitignore`);
each machine adds its own. With no `.crt` here the build uses the normal public roots only. After adding or changing
one, rebuild: `docker compose up -d --build`.

This folder is about the build and outbound connections. It is unrelated to the certificate Caddy serves to browsers
(Caddy's local CA by default; see the README, "HTTPS").
