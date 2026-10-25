# Web Search Plus 5.1.5

DonSeTch on Windows, and DonSeTch 4.10.2. No configuration changes are needed.

## Fixed

- **DonSeTch on Windows.** Every DonSeTch search and extract failed on Windows with `donsetch: Provider execution failed`. The adapter waited for DonSeTch's replies with `select()`, which cannot wait on a pipe on Windows. A reader thread per session now delivers the replies. Linux and macOS behave as before.
- The DonSeTch handshake sends the Web Search Plus version as `clientInfo.version`.
- A test that compared two cache hits including their age no longer fails now and then on a slow machine.

## Changed

- **DonSeTch 4.10.2** is the tested version (4.7.0 and newer still work). `docs/DONSETCH.md` now explains pointing `DONSETCH_BIN` at the binary instead of a `PATH` wrapper (two names for one install, e.g. on WSL2), and how to keep a private DonSeTch install for Web Search Plus so a global `npm i -g donsetch` upgrade does not change what it runs.

## Thanks

- [@pompousman](https://github.com/pompousman) for the Windows fix and its regression test ([#168](https://github.com/robbyczgw-cla/hermes-web-search-plus/pull/168)).
- [@friday-cp](https://github.com/friday-cp) for the WSL2 note, and [@Mart-Bogdan](https://github.com/Mart-Bogdan) for the hardening suggestions ([#167](https://github.com/robbyczgw-cla/hermes-web-search-plus/issues/167)).

Full list: [CHANGELOG](../CHANGELOG.md).
