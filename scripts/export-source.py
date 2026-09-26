#!/usr/bin/env python3
"""Export public source from a strict allowlist, never a working-tree archive."""
import argparse,hashlib,json,re,shutil
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BLOCKED={'node_modules','__pycache__','evidence','alpha','messages','data','.git','.runtime','test-output'}
SENSITIVE=re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|sk-[A-Za-z0-9_-]{24,}|[A-Za-z]:[/\\]Users[/\\][A-Za-z0-9_.-]+')
def sources(include_development=False):
    files={}
    groups=[('service',{'.py','.json'}),('web',{'.js','.css','.html','.svg'}),('desktop',{'.cjs','.html','.css','.json','.png','.ico'})]
    if include_development:groups += [('cloud',{'.py','.html','.css','.js'}),('.github',{'.yml','.yaml'})]
    for folder,suffixes in groups:
        for p in (ROOT/folder).rglob('*'):
            parts=p.relative_to(ROOT).parts
            if p.is_file() and p.suffix in suffixes and not set(parts)&BLOCKED and 'test' not in parts and p.name!='package-lock.json' and '.local.' not in p.name:
                files[p.relative_to(ROOT).as_posix()]=p.read_bytes()
    names=['agent-client.py','enroll-agent.py','update-control.py','verify-release.cjs','start-desktop.ps1','migrate-data.py','download-update.py']
    if include_development:names+=['build-release.py','export-source.py','build-macos.py','launcher.cs','package-smoke.py']
    for name in names:files['scripts/'+name]=(ROOT/'scripts'/name).read_bytes()
    for name in ['Start-Control.ps1','Start-Control.vbs']:files[name]=(ROOT/name).read_bytes()
    for name in ['README.md','AGENTS.md']:
        p=ROOT/'docs/distribution'/name
        files[name]=(p if p.exists() else ROOT/name).read_bytes()
    files['config/release-public.pem']=(ROOT/'config/release-public.pem').read_bytes()
    if include_development and (ROOT/'config/release-baseline.json').exists():files['config/release-baseline.json']=(ROOT/'config/release-baseline.json').read_bytes()
    if include_development:files['.gitignore']=b'data/\nruntime/\n.tools/\n.release/\nbuild/\n__pycache__/\n*.pyc\nnode_modules/\n*.sqlite*\nconfig/*.local.json\n*.pending\n'
    # Signed baselines describe bytes; Git must preserve the exported line endings.
    if include_development:files['.gitattributes']=b'* -text\n'
    for name,raw in files.items():
        if SENSITIVE.search(raw):raise ValueError('sensitive_content_in_'+name)
    return files
def export(target):
    if target.exists():raise ValueError('export_destination_exists')
    files=sources(True)
    for name,raw in files.items():
        p=target/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
    return {'files':len(files),'sha256':{k:hashlib.sha256(v).hexdigest() for k,v in files.items()}}
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('target',type=Path);args=parser.parse_args()
    print(json.dumps(export(args.target),indent=2))
