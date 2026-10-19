# Contributing

Thanks for helping. A few ground rules keep AgentWebView small and trustworthy:

1. **Run the tests**: `pip install -e '.[dev,browser]' && playwright install chromium && pytest`.
2. **Never weaken a guard to make something work.** The egress checks, the limit reservations, the approval for
   risky browser clicks and the dashboard's Host/header checks exist on purpose.
3. **Providers**: a new adapter follows the provider's official documentation (link it in the spec), meters in
   the provider's own unit, maps its "out of credit" and "bad key" answers, and comes with a test using a fake
   transport (`tests/conftest.py`). Never create accounts or claim credit on someone's behalf.
4. **Everything an agent can trigger is recorded** on its receipt. Keep it that way.
5. Keep the dashboard dependency-free: plain JavaScript, no build step, no external resources, and never insert
   retrieved content as HTML.

By contributing you agree that your contributions are licensed under the Apache License 2.0.
