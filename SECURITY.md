# Security

AgentWebView is a single-user tool with **no login**. Its security rests on running on your own computer:

- It listens on `127.0.0.1` by default and refuses other addresses unless started with `--allow-remote`.
  Never expose it to a network without your own authentication in front of it.
- Requests with an unknown `Host` header are refused; the dashboard API requires the `X-AgentWebView: 1` header and
  a same-site origin; agent endpoints require an agent token (stored as a SHA-256 hash).
- Provider keys are encrypted at rest with AES-256-GCM using `secret.key` in the data folder (mode 0600). Anyone who
  can read both the database and `secret.key` can read the keys: protect the data folder like a password store.
- Outbound requests from the page reader, the crawler and the browser proxy are checked at connection time:
  one DNS resolution per connection, every answer must be a public unicast address, only ports 80/443/8080/8443,
  redirects re-checked hop by hop. Provider calls go only to each provider's declared HTTPS hosts.
- The browser runs Chromium headless in a separate process group, all traffic through the proxy, limited to the
  session's hosts, with typed actions only and a person's approval before write-like clicks. In Docker it runs
  without Chromium's own sandbox; the container is the boundary there.
- Content retrieved from the web or from providers is untrusted data. Agents should treat it as such (prompt
  injection is possible in any web page); every receipt carries `retrieved_content_is_data: true`.

## Reporting a vulnerability

Please report security issues privately to the maintainers (open a GitHub security advisory on this repository)
rather than in a public issue. Include the version, how to reproduce, and the impact you see.
