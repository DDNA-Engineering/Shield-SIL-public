# Running the SIL in a container

The SIL needs a Linux host with root (network namespaces, taps, a CPU-cap cgroup).
`./sil` runs every step in a privileged container built from `docker/Dockerfile`,
so the only thing the host needs is Docker. The checkout is mounted at `/work`,
so edits to the harness, tests or overlays take effect without rebuilding, and
images and run logs land in `./build` as usual.

Works on a Linux host with Docker, and on Windows through WSL2: clone into the
WSL distro's own filesystem (for example `~/Shield-SIL`, not `/mnt/c` or `/mnt/d`,
which is far slower for 32 GB sparse disk images) and use Docker inside that distro
(native Docker Engine, or Docker Desktop with WSL integration).

```sh
./sil all                      # unit tests, build openwrt-24.10.3, boot it, system tests
./sil test ubuntu-26.04        # system tests on another image (builds it if missing)
./sil test openwrt-24.10.3 -k network   # extra args go to pytest
./sil up                       # boot and keep running; Ctrl-C stops
./sil ssh                      # in a second terminal: root shell on the Shield (OpenWrt)
./sil ssh ubus call shield status   # or one command there
./sil lan                      # in a second terminal: shell on the LAN client (192.168.100.50)
./sil lan curl -s http://192.168.100.1/   # or one command there
./sil images                   # catalog, and what's built
./sil shell                    # a root shell in the SIL container
```

From Windows PowerShell, prefix any of these with
`wsl -d Ubuntu --cd /home/<you>/Shield-SIL --`, for example
`wsl -d Ubuntu --cd /home/<you>/Shield-SIL -- ./sil all`.

Notes

- Every command streams build, boot and pytest output to the terminal (`pytest -v`).
- Each `./sil` command runs in its own container with its own network namespace,
  so `./sil test` can run while `./sil up` keeps a Shield running.
- The OpenWrt builder pulls `openwrt/*` images through the host's Docker socket
  (mounted into the container); it uses `docker create/cp/export` only, no bind mounts.
- Disk: the images are sparse, but plan on about 30 GB with every image built.
