#!/usr/bin/env python3
"""
Send Temperature_Chamber experiment folders from a Raspberry Pi to one PC.

Default use on each Raspberry Pi:

    python3 migrate_to.py

The script lists local experiment folders, asks which one to send, copies that
folder to the configured PC SMB share, shows progress in the Pi terminal, then
asks whether to delete the copied contents from the Pi.
"""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PC_HOST = "10.140.55.134"
DEFAULT_PC_USER = "Siheon"
DEFAULT_PC_TARGET = f"{DEFAULT_PC_USER}@{DEFAULT_PC_HOST}"
DEFAULT_PC_DIR = "."
DEFAULT_SHARE_DIR = "/mnt/chamber_data_share"
DEFAULT_SMB_SHARE = f"//{DEFAULT_PC_HOST}/chamber_data_share"

EXPERIMENT_PATTERNS = (
    "Temperature_*.csv",
    "SensorTime_*.csv",
    "Video_*.mp4",
    "TD_*_trial-wise.csv",
)


@dataclass(frozen=True)
class ExperimentFolder:
    path: Path
    base: Path
    rel: Path
    files: int
    bytes: int
    kind: str


class ProgressBar:
    def __init__(self, total: int, label: str = "Copying") -> None:
        self.total = max(total, 1)
        self.label = label
        self.current = 0
        self.started_at = time.time()
        self.last_draw = 0.0

    def update(self, amount: int) -> None:
        self.current += amount
        now = time.time()
        if now - self.last_draw >= 0.1 or self.current >= self.total:
            self.draw()
            self.last_draw = now

    def draw(self) -> None:
        width = 30
        ratio = min(self.current / self.total, 1.0)
        filled = int(width * ratio)
        bar = "#" * filled + "-" * (width - filled)
        elapsed = max(time.time() - self.started_at, 0.001)
        speed = self.current / elapsed
        sys.stdout.write(
            "\r"
            f"{self.label}: [{bar}] {ratio * 100:5.1f}% "
            f"{format_bytes(self.current)}/{format_bytes(self.total)} "
            f"({format_bytes(int(speed))}/s)"
        )
        sys.stdout.flush()

    def finish(self) -> None:
        self.current = self.total
        self.draw()
        sys.stdout.write("\n")
        sys.stdout.flush()


def shell_quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"Required command not found: {name}")


def default_search_bases() -> list[Path]:
    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    candidates = [
        cwd.parent,          # maintemp.py saves to ../<protocol>/<mouse_session>
        cwd,
        home / "Desktop",
        home,
    ]
    seen: set[Path] = set()
    bases: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        bases.append(resolved)
    return bases


def summarize_folder(path: Path) -> tuple[int, int]:
    file_count = 0
    byte_count = 0
    for root, dirnames, filenames in os.walk(path):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            file_path = Path(root) / name
            try:
                stat = file_path.stat()
            except OSError:
                continue
            file_count += 1
            byte_count += stat.st_size
    return file_count, byte_count


def has_experiment_file(filenames: list[str]) -> bool:
    return any(
        fnmatch.fnmatch(name, pattern)
        for name in filenames
        for pattern in EXPERIMENT_PATTERNS
    )


def depth_from(base: Path, path: Path) -> int:
    rel = path.relative_to(base)
    return len(rel.parts)


def add_candidate(
    candidates: dict[Path, ExperimentFolder],
    base: Path,
    path: Path,
    kind: str,
) -> None:
    if path == base or not path.is_dir():
        return
    try:
        key = path.resolve()
    except OSError:
        return
    files, bytes_ = summarize_folder(path)
    if files == 0:
        return
    rel = path.relative_to(base)
    existing = candidates.get(key)
    if existing is not None:
        if existing.kind != "session" and kind == "session":
            candidates[key] = ExperimentFolder(path, base, rel, files, bytes_, kind)
        return
    candidates[key] = ExperimentFolder(path, base, rel, files, bytes_, kind)


def discover_folders(args: argparse.Namespace) -> list[ExperimentFolder]:
    bases = [Path(item).expanduser().resolve() for item in args.search_base] if args.search_base else default_search_bases()
    candidates: dict[Path, ExperimentFolder] = {}

    for base in bases:
        if not base.is_dir():
            continue
        for root, dirnames, filenames in os.walk(base):
            root_path = Path(root)
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            if depth_from(base, root_path) >= args.max_depth:
                dirnames[:] = []

            if args.include_all_folders and root_path != base and filenames:
                add_candidate(candidates, base, root_path, "folder")

            if not has_experiment_file(filenames):
                continue

            add_candidate(candidates, base, root_path, "session")
            parent = root_path.parent
            if parent != base:
                add_candidate(candidates, base, parent, "parent")

    return sorted(candidates.values(), key=lambda folder: (str(folder.base), str(folder.rel).lower(), folder.kind))


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def print_folder_list(folders: list[ExperimentFolder]) -> None:
    print("\nLocal Raspberry Pi folders:")
    for index, folder in enumerate(folders, start=1):
        label = "session" if folder.kind == "session" else "folder"
        print(f"  {index:>2}. [{label}] {folder.rel}  ({folder.files} files, {format_bytes(folder.bytes)})")
        print(f"      {folder.path}")


def prompt_folder_choice(folders: list[ExperimentFolder]) -> ExperimentFolder:
    while True:
        raw = input("\nSend which folder to PC? Enter number, or q to quit: ").strip()
        if raw.lower() in {"q", "quit", "exit"}:
            raise SystemExit(0)
        try:
            index = int(raw)
        except ValueError:
            print("Please enter a number from the list.")
            continue
        if 1 <= index <= len(folders):
            return folders[index - 1]
        print("That number is not in the list.")


def run_printed(cmd: list[str]) -> None:
    print("+ " + " ".join(shell_quote(part) for part in cmd))
    subprocess.run(cmd, text=True, check=True)


def build_ssh_command(args: argparse.Namespace) -> str:
    parts = ["ssh"]
    if args.ssh_port:
        parts.extend(["-p", str(args.ssh_port)])
    if args.identity_file:
        parts.extend(["-i", str(args.identity_file)])
    for option in args.ssh_option:
        parts.extend(["-o", option])
    return " ".join(shell_quote(part) for part in parts)


def build_ssh_base(args: argparse.Namespace) -> list[str]:
    cmd = ["ssh"]
    if args.ssh_port:
        cmd.extend(["-p", str(args.ssh_port)])
    if args.identity_file:
        cmd.extend(["-i", str(args.identity_file)])
    for option in args.ssh_option:
        cmd.extend(["-o", option])
    return cmd


def build_scp_base(args: argparse.Namespace) -> list[str]:
    cmd = ["scp"]
    if args.ssh_port:
        cmd.extend(["-P", str(args.ssh_port)])
    if args.identity_file:
        cmd.extend(["-i", str(args.identity_file)])
    for option in args.ssh_option:
        cmd.extend(["-o", option])
    return cmd


def remote_supports_rsync(args: argparse.Namespace) -> bool:
    cmd = [*build_ssh_base(args), args.target, "command -v rsync >/dev/null 2>&1"]
    return subprocess.run(cmd, text=True).returncode == 0


def copy_to_pc_ssh(args: argparse.Namespace, folder: ExperimentFolder) -> None:
    pc_dir = args.pc_dir.rstrip("/") or "."
    use_rsync = args.ssh_tool == "rsync" or (
        args.ssh_tool == "auto" and shutil.which("rsync") and remote_supports_rsync(args)
    )

    if use_rsync:
        require_tool("rsync")
        remote_folder = f"{pc_dir.rstrip('/')}/{folder.path.name}"
        cmd = [
            "rsync",
            "-ah",
            "--info=progress2",
            "--partial",
            "-e",
            build_ssh_command(args),
            str(folder.path) + os.sep,
            f"{args.target}:{shell_quote(remote_folder.rstrip('/') + '/')}",
        ]
        run_printed(cmd)
        return

    require_tool("scp")
    print("Using scp. Most OpenSSH scp versions show per-file progress automatically.")
    cmd = [
        *build_scp_base(args),
        "-r",
        str(folder.path),
        f"{args.target}:{pc_dir}",
    ]
    run_printed(cmd)


def copy_file_with_progress(src: Path, dst: Path, progress: ProgressBar) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as reader, dst.open("wb") as writer:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            writer.write(chunk)
            progress.update(len(chunk))
    shutil.copystat(src, dst, follow_symlinks=True)


def copy_to_share(args: argparse.Namespace, folder: ExperimentFolder) -> None:
    share_dir = Path(args.share_dir).expanduser()
    if not share_dir.is_dir():
        raise SystemExit(
            f"Share directory is not available: {share_dir}\n"
            "Mount the PC shared folder on the Raspberry Pi first, or use --method ssh."
        )

    destination_root = share_dir / folder.path.name
    progress = ProgressBar(folder.bytes, label="Copying to PC share")
    for root, dirnames, filenames in os.walk(folder.path):
        root_path = Path(root)
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        rel_root = root_path.relative_to(folder.path)
        (destination_root / rel_root).mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            src = root_path / filename
            dst = destination_root / rel_root / filename
            copy_file_with_progress(src, dst, progress)
    progress.finish()
    print(f"Copied to mounted PC share: {destination_root}")


def is_mountpoint(path: Path) -> bool:
    return subprocess.run(["mountpoint", "-q", str(path)], text=True).returncode == 0


def get_smb_password(args: argparse.Namespace) -> str:
    if args.smb_password_env:
        password = os.environ.get(args.smb_password_env)
        if password is not None:
            return password
    return getpass.getpass(f"SMB password for {args.smb_user}: ")


def write_smb_credentials(args: argparse.Namespace) -> Path:
    password = get_smb_password(args)
    handle = tempfile.NamedTemporaryFile("w", prefix="migrate_smb_", delete=False)
    credentials_path = Path(handle.name)
    try:
        handle.write(f"username={args.smb_user}\n")
        handle.write(f"password={password}\n")
        if args.smb_domain:
            handle.write(f"domain={args.smb_domain}\n")
        handle.close()
        credentials_path.chmod(0o600)
        return credentials_path
    except Exception:
        handle.close()
        credentials_path.unlink(missing_ok=True)
        raise


def mount_smb_share(args: argparse.Namespace) -> bool:
    require_tool("sudo")
    mountpoint = Path(args.share_dir).expanduser()
    if mountpoint.is_dir() and is_mountpoint(mountpoint):
        print(f"Using already mounted share: {mountpoint}")
        return False

    if not any(Path(path).exists() for path in ("/sbin/mount.cifs", "/usr/sbin/mount.cifs")) and shutil.which("mount.cifs") is None:
        raise SystemExit("mount.cifs not found. On Raspberry Pi, install it with: sudo apt install cifs-utils")

    run_printed(["sudo", "mkdir", "-p", str(mountpoint)])
    credentials_path = write_smb_credentials(args)
    options = [
        f"credentials={credentials_path}",
        f"uid={os.getuid()}",
        f"gid={os.getgid()}",
        "vers=3.0",
        "iocharset=utf8",
        "noperm",
    ]
    if args.smb_options:
        options.extend(item for item in args.smb_options.split(",") if item)
    try:
        run_printed([
            "sudo",
            "mount",
            "-t",
            "cifs",
            args.smb_share,
            str(mountpoint),
            "-o",
            ",".join(options),
        ])
    finally:
        credentials_path.unlink(missing_ok=True)

    return True


def unmount_smb_share(args: argparse.Namespace) -> None:
    mountpoint = Path(args.share_dir).expanduser()
    if not is_mountpoint(mountpoint):
        return
    result = subprocess.run(["sudo", "umount", str(mountpoint)], text=True)
    if result.returncode == 0:
        print(f"Unmounted PC share: {mountpoint}")
    else:
        print(f"Warning: failed to unmount {mountpoint}. You can run: sudo umount {mountpoint}")


def select_method(args: argparse.Namespace) -> str:
    if args.method != "auto":
        return args.method
    if Path(args.share_dir).expanduser().is_dir():
        return "share"
    return "smb"


def prompt_delete_local_contents(folder: ExperimentFolder) -> None:
    print(f"\nCopied local Raspberry Pi folder:\n  {folder.path}")
    raw = input("Delete the copied contents from this Raspberry Pi folder? [y/N]: ").strip().lower()
    if raw not in {"y", "yes"}:
        print("Local Raspberry Pi files were left in place.")
        return

    path = folder.path.resolve()
    home = Path.home().resolve()
    if path in {Path("/"), home} or len(path.parts) < 3:
        raise SystemExit(f"Refusing to delete unsafe path: {path}")

    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    print("Local Raspberry Pi folder contents deleted.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run on each Raspberry Pi to send a selected experiment folder to the lab PC.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--method",
        choices=("auto", "share", "smb", "ssh"),
        default="auto",
        help="auto uses an existing mounted share when available, otherwise mounts the PC SMB share temporarily. Use ssh only as fallback.",
    )
    parser.add_argument(
        "--share-dir",
        default=os.environ.get("MIGRATE_SHARE_DIR", DEFAULT_SHARE_DIR),
        help="Mounted PC shared folder path on the Raspberry Pi for --method share or --method smb.",
    )
    parser.add_argument(
        "--smb-share",
        default=os.environ.get("MIGRATE_SMB_SHARE", DEFAULT_SMB_SHARE),
        help="Windows/SMB share to mount temporarily for --method smb.",
    )
    parser.add_argument(
        "--smb-user",
        default=os.environ.get("MIGRATE_SMB_USER", DEFAULT_PC_USER),
        help="SMB username for --method smb.",
    )
    parser.add_argument(
        "--smb-domain",
        default=os.environ.get("MIGRATE_SMB_DOMAIN", ""),
        help="Optional SMB domain/workgroup for --method smb.",
    )
    parser.add_argument(
        "--smb-password-env",
        default="MIGRATE_SMB_PASSWORD",
        help="Environment variable containing the SMB password. If absent, the script prompts.",
    )
    parser.add_argument(
        "--smb-options",
        default=os.environ.get("MIGRATE_SMB_OPTIONS", ""),
        help="Extra comma-separated CIFS mount options.",
    )
    parser.add_argument(
        "--keep-mounted",
        action="store_true",
        help="With --method smb, leave the share mounted after copying.",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("MIGRATE_TARGET", DEFAULT_PC_TARGET),
        help="PC SSH target for --method ssh.",
    )
    parser.add_argument(
        "--pc-dir",
        default=os.environ.get("MIGRATE_PC_DIR", DEFAULT_PC_DIR),
        help="Destination directory on the PC for --method ssh.",
    )
    parser.add_argument(
        "--ssh-tool",
        choices=("auto", "rsync", "scp"),
        default="auto",
        help="SSH transfer tool. rsync shows an overall progress bar; scp is the fallback.",
    )
    parser.add_argument("--ssh-port", type=int, default=None, help="PC SSH port for --method ssh.")
    parser.add_argument("--identity-file", default=None, help="SSH private key path for --method ssh.")
    parser.add_argument("--ssh-option", action="append", default=[], help="Extra SSH -o option. Repeat for multiple options.")
    parser.add_argument(
        "--search-base",
        action="append",
        help="Local Raspberry Pi directory to search. Repeat to search several bases. Default includes the project parent, project folder, ~/Desktop, and ~.",
    )
    parser.add_argument("--max-depth", type=int, default=4, help="How deep to search below each local base.")
    parser.add_argument(
        "--include-all-folders",
        action="store_true",
        help="Also list non-empty folders that do not match the usual experiment file names.",
    )
    args = parser.parse_args(argv)
    if args.max_depth < 1:
        parser.error("--max-depth must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    folders = discover_folders(args)
    if not folders and not args.include_all_folders:
        print("No experiment-like folders were found.")
        print("Try again with --include-all-folders or specify --search-base.")
        return 1
    if not folders:
        print("No folders were found.")
        return 1

    print_folder_list(folders)
    selected = prompt_folder_choice(folders)
    method = select_method(args)
    if method == "share":
        print(f"\nSending through mounted PC share: {args.share_dir}")
        copy_to_share(args, selected)
    elif method == "smb":
        print(f"\nMounting PC SMB share temporarily: {args.smb_share} -> {args.share_dir}")
        mounted_by_script = mount_smb_share(args)
        try:
            copy_to_share(args, selected)
        finally:
            if mounted_by_script and not args.keep_mounted:
                unmount_smb_share(args)
    else:
        print(f"\nSending to PC over SSH: {args.target}:{args.pc_dir}")
        copy_to_pc_ssh(args, selected)
    prompt_delete_local_contents(selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
