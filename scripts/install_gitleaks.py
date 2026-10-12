"""Install an exact upstream release only after its pinned archive checksum passes."""

import hashlib
import io
import platform
import tarfile
from pathlib import Path

import httpx

VERSION = "8.30.1"
ARCHIVES = {
    ("Darwin", "arm64"): (
        "darwin_arm64",
        "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
    ),
    ("Linux", "x86_64"): (
        "linux_x64",
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    ),
}


def main():
    choice = ARCHIVES.get((platform.system(), platform.machine()))
    if choice is None:
        raise SystemExit("No reviewed Gitleaks archive for this platform")
    name, digest = choice
    url = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/gitleaks_{VERSION}_{name}.tar.gz"
    response = httpx.get(url, follow_redirects=True, timeout=60)
    response.raise_for_status()
    data = response.content
    if hashlib.sha256(data).hexdigest() != digest:
        raise SystemExit("Gitleaks archive checksum mismatch; refusing execution")
    target = Path(__file__).resolve().parents[1] / ".local/tools/gitleaks"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.is_symlink():
        raise SystemExit("Tool target cannot be a symlink")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember("gitleaks")
        if not member.isfile():
            raise SystemExit("Gitleaks archive does not contain the expected regular binary")
        stream = archive.extractfile(member)
        if stream is None:
            raise SystemExit("Gitleaks binary is unavailable")
        target.write_bytes(stream.read())
    target.chmod(0o700)
    print(f"Verified Gitleaks {VERSION} {name} archive SHA256 {digest}")


if __name__ == "__main__":
    main()
