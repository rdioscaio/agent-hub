"""Remote execution tools via SSH.

Allows agents to operate on configured remote hosts (e.g. maincua-prod)
without requiring a local VSCode session. Host aliases resolve via ~/.ssh/config.

Tools: remote_exec, remote_read_file, remote_write_file, remote_list_dir,
       remote_explore_project
"""

import shlex
import subprocess

from hub.audit import audit

# Hard caps
_MAX_OUTPUT_BYTES = 256 * 1024   # 256 KB — stdout/stderr cap
_MAX_WRITE_BYTES  = 512 * 1024   # 512 KB — write cap (same as artifacts)
_DEFAULT_TIMEOUT  = 30           # seconds

_KNOWN_HOSTS = {"maincua-prod"}

# SSH options that ensure non-interactive, predictable behavior in automation
_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def _check_host(host_alias: str) -> dict | None:
    """Return error dict if host_alias is not in allowlist, else None."""
    if host_alias not in _KNOWN_HOSTS:
        return {
            "ok": False,
            "error": f"unknown host_alias '{host_alias}'. Known: {sorted(_KNOWN_HOSTS)}",
        }
    return None


def _ssh_run(host_alias: str, command: str, timeout: int) -> dict:
    """Low-level SSH execution. Returns dict with stdout, stderr, exit_code.

    Includes `truncated: True` when output was cut by the 256 KB cap.
    """
    err = _check_host(host_alias)
    if err:
        return err
    try:
        result = subprocess.run(
            ["ssh", *_SSH_OPTS, host_alias, command],
            capture_output=True,
            timeout=timeout,
        )
        stdout_raw = result.stdout
        stderr_raw = result.stderr
        truncated = len(stdout_raw) > _MAX_OUTPUT_BYTES or len(stderr_raw) > _MAX_OUTPUT_BYTES
        stdout = stdout_raw[:_MAX_OUTPUT_BYTES].decode(errors="replace")
        stderr = stderr_raw[:_MAX_OUTPUT_BYTES].decode(errors="replace")
        out = {
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
        if truncated:
            out["truncated"] = True
        return out
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"command timed out after {timeout}s"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def remote_exec(
    host_alias: str,
    command: str,
    working_dir: str = "",
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict:
    """Execute a shell command on a remote host via SSH.

    Args:
        host_alias:  SSH config alias (e.g. 'maincua-prod').
        command:     Shell command to run.
        working_dir: Optional directory to cd into before running.
        timeout:     Seconds before aborting (default 30).

    Returns dict with ok, exit_code, stdout, stderr.
    """
    args = dict(host_alias=host_alias, command=command, working_dir=working_dir)
    with audit("remote_exec", args):
        full_cmd = f"cd {shlex.quote(working_dir)} && {command}" if working_dir else command
        return _ssh_run(host_alias, full_cmd, timeout)


def remote_read_file(
    host_alias: str,
    path: str,
    max_lines: int = 0,
) -> dict:
    """Read a file from a remote host.

    Args:
        host_alias: SSH config alias.
        path:       Absolute path to the file.
        max_lines:  If > 0, return only the first N lines.

    Returns dict with ok, content (str), lines (int).
    """
    args = dict(host_alias=host_alias, path=path, max_lines=max_lines)
    with audit("remote_read_file", args):
        if max_lines > 0:
            cmd = f"head -n {int(max_lines)} {shlex.quote(path)}"
        else:
            cmd = f"cat {shlex.quote(path)}"
        result = _ssh_run(host_alias, cmd, _DEFAULT_TIMEOUT)
        if not result["ok"]:
            return result
        content = result["stdout"]
        return {"ok": True, "content": content, "lines": content.count("\n")}


def remote_write_file(
    host_alias: str,
    path: str,
    content: str,
    create_dirs: bool = True,
) -> dict:
    """Write (overwrite) a file on a remote host.

    Args:
        host_alias:  SSH config alias.
        path:        Absolute path to write.
        content:     File content (text).
        create_dirs: If True, create parent directories as needed (default True).

    Returns dict with ok, path, bytes_written.
    """
    args = dict(host_alias=host_alias, path=path, create_dirs=create_dirs)
    with audit("remote_write_file", args):
        err = _check_host(host_alias)
        if err:
            return err
        encoded = content.encode()
        if len(encoded) > _MAX_WRITE_BYTES:
            return {"ok": False, "error": f"content exceeds {_MAX_WRITE_BYTES // 1024} KB limit"}
        try:
            import pathlib
            mkdir_cmd = f"mkdir -p {shlex.quote(str(pathlib.Path(path).parent))} && " if create_dirs else ""
            result = subprocess.run(
                ["ssh", *_SSH_OPTS, host_alias, f"{mkdir_cmd}cat > {shlex.quote(path)}"],
                input=encoded,
                capture_output=True,
                timeout=_DEFAULT_TIMEOUT,
            )
            if result.returncode != 0:
                stderr = result.stderr[:_MAX_OUTPUT_BYTES].decode(errors="replace")
                return {"ok": False, "error": stderr or "write failed", "exit_code": result.returncode}
            return {"ok": True, "path": path, "bytes_written": len(encoded)}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"write timed out after {_DEFAULT_TIMEOUT}s"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def remote_list_dir(
    host_alias: str,
    path: str,
    show_hidden: bool = False,
) -> dict:
    """List directory contents on a remote host.

    Uses find -printf for machine-readable output — preserves filenames with
    spaces and handles symlinks correctly.

    Args:
        host_alias:  SSH config alias.
        path:        Directory path to list.
        show_hidden: Include dotfiles (default False).

    Returns dict with ok, path, entries (list of {name, type, size}).
    """
    args = dict(host_alias=host_alias, path=path, show_hidden=show_hidden)
    with audit("remote_list_dir", args):
        hidden_filter = "" if show_hidden else r"-not -name '.*'"
        # No 2>&1 — keep stderr separate so exit code from find is preserved.
        # Sorting happens in Python to avoid the pipeline swallowing find's exit code.
        cmd = (
            f"find {shlex.quote(path)} -mindepth 1 -maxdepth 1 "
            f"{hidden_filter} -printf '%y %s %f\\n'"
        )
        result = _ssh_run(host_alias, cmd, _DEFAULT_TIMEOUT)
        if not result["ok"]:
            return result
        entries = []
        for line in sorted(result["stdout"].splitlines()):
            # format: <type_char> <size> <name>  (name may contain spaces)
            parts = line.split(" ", 2)
            if len(parts) < 3:
                continue
            ftype_char, size, name = parts
            if not name or name == ".":
                continue
            ftype = "dir" if ftype_char == "d" else "link" if ftype_char == "l" else "file"
            entries.append({"name": name, "type": ftype, "size": size})
        return {"ok": True, "path": path, "entries": entries, "count": len(entries)}


def remote_explore_project(
    host_alias: str,
    project_path: str,
) -> dict:
    """Explore a project on a remote host: structure, git status, README excerpt.

    Designed to give an agent enough context to start working on a project
    without needing a local clone or VSCode session.

    Returns ok=False if the host is unknown or unreachable. Returns ok=True
    with partial/empty fields if the project path doesn't exist or has no git
    history — that is valid information about the project state.

    Args:
        host_alias:    SSH config alias.
        project_path:  Absolute path to the project root.

    Returns dict with ok, project_path, structure, git (log + status),
            readme, package_files. On host failure: ok=False, error.
    """
    args = dict(host_alias=host_alias, project_path=project_path)
    with audit("remote_explore_project", args):
        # Allowlist check before any subprocess
        err = _check_host(host_alias)
        if err:
            return err

        # Connectivity pre-flight — fail fast with clear error before running 5 commands
        probe = _ssh_run(host_alias, "true", timeout=10)
        if not probe["ok"]:
            return {
                "ok": False,
                "error": f"host unreachable: {probe.get('error') or probe.get('stderr', '')}",
                "host_alias": host_alias,
            }

        p = shlex.quote(project_path)

        # Check whether project_path exists on the remote before running 5 commands.
        # path_exists=False is valid information (caller can act on it); it is NOT a
        # connectivity failure, so ok stays True and we return immediately with empty fields.
        path_check = _ssh_run(host_alias, f"test -d {p}", timeout=10)
        if not path_check["ok"]:
            return {
                "ok": True,
                "project_path": project_path,
                "path_exists": False,
                "structure": "",
                "git": {"log": "", "status": ""},
                "readme": "",
                "package_files": [],
                "warnings": [f"project_path does not exist or is not a directory: {project_path}"],
            }

        # Top-level structure (2 levels, no node_modules/.git/venv).
        # No pipeline — sort and truncate in Python to preserve find's exit code.
        tree_cmd = (
            f"find {p} -maxdepth 2 -not -path '*/node_modules/*' "
            f"-not -path '*/.git/*' -not -path '*/venv/*' "
            f"-not -path '*/__pycache__/*'"
        )

        # Git info
        git_log_cmd = f"cd {p} && git log --oneline -10 2>/dev/null || echo '(not a git repo)'"
        git_status_cmd = f"cd {p} && git status --short 2>/dev/null || true"

        # README
        readme_cmd = (
            f"f=$(ls {p}/README* {p}/readme* 2>/dev/null | head -1); "
            f"[ -f \"$f\" ] && head -100 \"$f\" || echo '(no README found)'"
        )

        # Known config/package files
        pkg_cmd = (
            f"ls {p}/package.json {p}/pyproject.toml {p}/requirements*.txt "
            f"{p}/Dockerfile {p}/docker-compose*.yml {p}/.env.example 2>/dev/null || true"
        )

        tree    = _ssh_run(host_alias, tree_cmd,        _DEFAULT_TIMEOUT)
        git_log = _ssh_run(host_alias, git_log_cmd,     _DEFAULT_TIMEOUT)
        git_st  = _ssh_run(host_alias, git_status_cmd,  _DEFAULT_TIMEOUT)
        readme  = _ssh_run(host_alias, readme_cmd,      _DEFAULT_TIMEOUT)
        pkgs    = _ssh_run(host_alias, pkg_cmd,         _DEFAULT_TIMEOUT)

        warnings = []
        for label, res in [("structure", tree), ("git_log", git_log), ("git_status", git_st),
                           ("readme", readme), ("package_files", pkgs)]:
            if not res["ok"]:
                detail = (
                    res.get("error")
                    or res.get("stderr", "").strip()
                    or f"exit_code={res.get('exit_code', '?')}"
                )
                warnings.append(f"{label}: {detail}")

        structure_lines = sorted(tree.get("stdout", "").splitlines())[:80]

        result = {
            "ok": True,
            "project_path": project_path,
            "path_exists": True,
            "structure": "\n".join(structure_lines),
            "git": {
                "log": git_log.get("stdout", ""),
                "status": git_st.get("stdout", ""),
            },
            "readme": readme.get("stdout", ""),
            "package_files": [
                ln.strip()
                for ln in pkgs.get("stdout", "").splitlines()
                if ln.strip()
            ],
        }
        if warnings:
            result["warnings"] = warnings
        return result
