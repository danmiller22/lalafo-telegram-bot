"""Install a pinned Tailscale client into Koyeb's ephemeral filesystem."""
from __future__ import annotations

import hashlib
import platform
import tarfile
import tempfile
import urllib.request
from pathlib import Path


VERSION = "1.104.2"
ARCHIVES = {
    "x86_64": ("amd64", "512f613ed7d04fc7282f7dc5bd76aa105aae28168fbb9357eb749ebcbc29f0ae"),
    "aarch64": ("arm64", "5776aaa901b5ef1916e66be5bb54b9f481cd399c9c28db123a8cab620efa237b"),
}
DESTINATION = Path("/tmp/tailscale-bin")


def main() -> None:
    machine = platform.machine().lower()
    if machine not in ARCHIVES:
        raise RuntimeError(f"Unsupported Koyeb architecture: {machine}")
    if (DESTINATION / "tailscale").is_file() and (DESTINATION / "tailscaled").is_file():
        return
    archive_arch, expected_sha256 = ARCHIVES[machine]
    url = f"https://pkgs.tailscale.com/stable/tailscale_{VERSION}_{archive_arch}.tgz"
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "tailscale.tgz"
        urllib.request.urlretrieve(url, archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != expected_sha256:
            raise RuntimeError("Tailscale archive checksum mismatch")
        with tarfile.open(archive, "r:gz") as bundle:
            members = {
                Path(member.name).name: member
                for member in bundle.getmembers()
                if Path(member.name).name in {"tailscale", "tailscaled"}
            }
            if set(members) != {"tailscale", "tailscaled"}:
                raise RuntimeError("Tailscale archive does not contain expected binaries")
            DESTINATION.mkdir(mode=0o700, parents=True, exist_ok=True)
            for name, member in members.items():
                source = bundle.extractfile(member)
                if source is None:
                    raise RuntimeError(f"Could not extract {name}")
                target = DESTINATION / name
                target.write_bytes(source.read())
                target.chmod(0o700)


if __name__ == "__main__":
    main()
