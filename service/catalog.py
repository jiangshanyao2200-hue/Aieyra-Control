"""显式登记来源的资源元信息导入；不复制SSH配置、认证或原文。"""

from __future__ import annotations
import configparser
import hashlib
import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def server_inventory(source):
    path = Path(source["path"])
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    allowed = ("服务器", "地址", "到期日期", "当前服务", "可用磁盘")
    rows = []
    for index in range(len(lines) - 1):
        if not lines[index].startswith("|") or not re.match(
            r"^\s*\|(?:\s*:?-+:?\s*\|)+\s*$", lines[index + 1]
        ):
            continue
        headers = [x.strip() for x in lines[index].strip("| ").split("|")]
        if not all(k in headers for k in ("服务器", "地址")):
            continue
        for line in lines[index + 2 :]:
            if not line.startswith("|"):
                break
            cells = [x.strip() for x in line.strip("| ").split("|")]
            if len(cells) != len(headers):
                continue
            values = {k: cells[headers.index(k)][:500] for k in allowed if k in headers}
            match = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", values["地址"])
            if not match:
                continue
            address = str(ipaddress.ip_address(match[0]))
            rows.append(
                {
                    "id": "server-" + address.replace(".", "-"),
                    "name": values["服务器"],
                    "kind": "server",
                    "project": source.get("project", "coordination"),
                    "location": "ssh://" + address + ":22",
                    "summary": values.get("当前服务", "") + "；清单记录，未作实时探测。",
                    "details": values,
                    "source_ref": str(path),
                    "source_sha256": digest(path),
                }
            )
    return rows


def ssh_endpoints(source):
    path = Path(source["path"])
    groups = []
    current = None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[0].startswith("#"):
            continue
        key, value = parts
        if key.lower() == "host":
            if current:
                groups.append(current)
            current = {"aliases": value}
        elif current is not None and key.lower() in ("hostname", "port"):
            current[key.lower()] = value
    if current:
        groups.append(current)
    rows = []
    for group in groups:
        host = group.get("hostname", "")
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", host) or not group.get("port", "22").isdigit():
            continue
        rows.append(
            {
                "id": "ssh-"
                + hashlib.sha256((host + ":" + group.get("port", "22")).encode()).hexdigest()[:16],
                "name": group["aliases"].split()[0],
                "kind": "server",
                "project": source.get("project", "coordination"),
                "location": "ssh://" + host + ":" + group.get("port", "22"),
                "summary": "SSH端点登记；未连接或验证远端。",
                "details": {"aliases": group["aliases"]},
                "source_ref": str(path),
            }
        )
    unique = {}
    for row in rows:
        if row["id"] in unique:
            unique[row["id"]]["details"]["aliases"] += " " + row["details"]["aliases"]
        else:
            unique[row["id"]] = row
    return list(unique.values())


def git_remote(source):
    path = Path(source["path"]) / ".git/config"
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(path, encoding="utf-8")
    rows = []
    for section in parser.sections():
        if not section.startswith("remote "):
            continue
        value = parser.get(section, "url", fallback="")
        match = re.fullmatch(r"git@github\.com:([A-Za-z0-9_./-]+)", value)
        if match:
            url = "https://github.com/" + match[1]
        else:
            parts = urlsplit(value)
            if parts.hostname != "github.com" or not re.fullmatch(r"/[A-Za-z0-9_./-]+", parts.path):
                continue
            url = "https://github.com" + parts.path
        rows.append(
            {
                "id": "github-" + hashlib.sha256(url.encode()).hexdigest()[:16],
                "name": url.rsplit("/", 1)[-1].removesuffix(".git"),
                "kind": "github",
                "project": source.get("project", ""),
                "location": url,
                "summary": "本地仓库登记的GitHub远端；尚未联网核对。",
                "source_ref": str(Path(source["path"])),
            }
        )
    return rows


def collect(sources):
    rows, errors = [], []
    handlers = {
        "server_inventory": server_inventory,
        "ssh_endpoints": ssh_endpoints,
        "git_remote": git_remote,
    }
    for source in sources:
        try:
            rows.extend(handlers[source["kind"]](source))
        except (OSError, ValueError, KeyError, configparser.Error):
            errors.append({"source": source.get("path"), "state": "unavailable"})
    merged = {}
    for row in rows:
        key = (row["kind"], row["location"])
        if key not in merged:
            merged[key] = row
        else:
            existing = merged[key]
            existing.setdefault("details", {}).update(row.get("details", {}))
            if row.get("source_ref") and row["source_ref"] != existing.get("source_ref"):
                existing["source_ref"] = existing.get("source_ref", "") + " ; " + row["source_ref"]
    return list(merged.values()), errors
