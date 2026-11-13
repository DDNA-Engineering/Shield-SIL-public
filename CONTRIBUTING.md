# Contributing

Thanks for helping. Issues and pull requests are welcome. By submitting a
contribution you agree to license it under the [Apache License 2.0](LICENSE).

## Set up

Follow the quick start in the [README](README.md). Unit tests need only Python;
system tests need root, QEMU, Docker and a built image.

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest tests/unit
sudo -E image/build.sh openwrt-24.10.3
sudo -E .venv/bin/python -m pytest tests/system --sil-image openwrt-24.10.3
```

## Pull requests

- Keep each change focused, and add or update a test that asserts the behaviour.
- `tests/unit` must pass; GitHub CI runs it with lint and the other quick checks.
  CI does not build images or run `tests/system`: for changes to the harness,
  models or images, run `./sil test <image>` locally on the images you touch and
  give the result in the pull request.
- A known gap is an `xfail(strict=True)` test with a reason, not a skipped or
  deleted one.
- Update [docs/validation-status.md](docs/validation-status.md) when you change
  what is tested, and [docs/assumptions.md](docs/assumptions.md) when you add or
  close a model assumption. Say where each model value comes from.

## What not to commit

- Built images, downloads, packages (`.ipk`, `.deb`) or other binaries.
- Credentials, tokens, private keys or `.env` files.
- Material you do not have the right to publish, such as code or documents from
  private repositories, NDA datasheets or internal notes. Model values should
  come from public datasheets, upstream source or your own measurements.

## Hardware claims

Never describe a SIL result as a hardware measurement. A result checked only in
the SIL says so; a result compared with a real unit links the capture or log.
