#!/usr/bin/env python3
"""Fetch official update files with the current signed-in account, then verify."""
import argparse,hashlib,json,os,sys,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'service'))
from cloud_session import SessionVault
from cloud_link import CloudLink,ORIGIN,NoRedirect
from paths import data_root
from release_verify import verify

def download(target):
    link=CloudLink(vault=SessionVault(data_root()/'config'));session=link.authenticated()
    envelope=link.check();manifest=verify(envelope);asset=manifest['source']
    if not asset['path'].startswith('/artifacts/') or '/' in asset['path'][11:]:raise ValueError('invalid_artifact_path')
    if target.exists():raise ValueError('update_directory_exists')
    target.mkdir(parents=True)
    destination=target/'source.zip';temporary=target/'source.pending';sha=hashlib.sha256();size=0
    request=urllib.request.Request(ORIGIN+asset['path'],headers={'Authorization':'Bearer '+session['access_token']})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=30) as r,temporary.open('xb') as f:
            while True:
                data=r.read(65536)
                if not data:break
                size+=len(data)
                if size>asset['size']:raise ValueError('download_size_limit')
                sha.update(data);f.write(data)
        if size!=asset['size'] or sha.hexdigest()!=asset['sha256']:raise ValueError('download_verification_failed')
        os.replace(temporary,destination);(target/'stable.json').write_text(json.dumps(envelope,ensure_ascii=False),encoding='utf-8')
    finally:temporary.unlink(missing_ok=True)
    return {'version':manifest['version'],'verified':True,'directory':str(target)}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();print(json.dumps(download(a.output)))
