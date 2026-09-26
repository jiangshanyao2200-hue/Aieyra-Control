#!/usr/bin/env python3
"""Agent-reviewed signed B/L/N updates; explicit choices, journal and rollback."""
import argparse,hashlib,json,os,shutil,sys,time,zipfile
from pathlib import Path,PurePosixPath

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'service'))
from release_verify import verify
from main import InstanceLock
from paths import shared_directory

def hashed(path):return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
def safe(root,name):
    p=PurePosixPath(name)
    if p.is_absolute() or '\\' in name or ':' in name or any(x in ('','..','.') for x in p.parts):raise ValueError('unsafe_update_path')
    if p.parts[0] not in ('service','web','desktop','scripts','config','docs','Start-Control.ps1','Start-Control.vbs','README.md','AGENTS.md','SECURITY.md','.gitignore'):raise ValueError('protected_update_path')
    if any(x in ('node_modules','__pycache__','evidence','.runtime') for x in p.parts) or '.local.' in name or name.endswith('.sqlite'):raise ValueError('protected_update_path')
    if name == 'config/release-baseline.json':raise ValueError('trust_root_requires_separate_rotation')
    candidate=root.joinpath(*p.parts)
    for parent in [candidate,*candidate.parents]:
        if parent==root.parent:break
        if parent.is_symlink():raise ValueError('symlink_in_update_path')
    if not candidate.resolve().is_relative_to(root.resolve()):raise ValueError('outside_installation')
    return candidate
def read(path):return json.loads(path.read_text(encoding='utf-8'))
def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.pending')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,path)
def prepare(root,manifest_path,archive,work):
    envelope=read(manifest_path);new=verify(envelope,root)
    baseline_path=root/'config/release-baseline.json'
    if not baseline_path.exists():raise ValueError('trusted_installation_baseline_required')
    base=verify(read(baseline_path),root)
    if new['sequence']<=base['sequence']:raise ValueError('non_forward_release')
    if hashed(archive)!=new['source']['sha256']:raise ValueError('archive_digest_mismatch')
    if work.exists():raise ValueError('candidate_directory_exists')
    work.mkdir(parents=True);write(work/'envelope.json',envelope)
    expected=new['files'];prior=base['files']
    if expected.get('config/release-public.pem',{}).get('sha256')!=hashed(root/'config/release-public.pem'):
        raise ValueError('trust_root_requires_separate_rotation')
    if not isinstance(expected,dict) or len(expected)>3000:raise ValueError('invalid_file_manifest')
    with zipfile.ZipFile(archive) as z:
        entries=z.infolist()
        if len({e.filename for e in entries})!=len(entries) or set(e.filename for e in entries)!=set(expected):raise ValueError('archive_entries_mismatch')
        if sum(e.file_size for e in entries)>100*1024*1024:raise ValueError('archive_size_limit')
        for entry in entries:
            name=entry.filename;safe(root,name);target=safe(work/'candidate',name)
            if entry.file_size!=expected[name]['size'] or entry.external_attr>>16&0o170000==0o120000:raise ValueError('invalid_archive_entry')
            data=z.read(entry)
            if hashlib.sha256(data).hexdigest()!=expected[name]['sha256']:raise ValueError('file_digest_mismatch')
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    rows=[]
    for name in sorted(set(prior)|set(expected)):
        local=hashed(safe(root,name));b=prior.get(name,{}).get('sha256');n=expected.get(name,{}).get('sha256')
        category='unchanged' if b==local==n else 'already_current' if local==n else 'local_only' if b==n else 'upstream_only' if local==b else 'conflict'
        rows.append({'path':name,'base':b,'local':local,'new':n,'category':category,
                     'choice':'keep' if category in ('unchanged','already_current','local_only') else 'review'})
    plan={'schema':1,'version':new['version'],'sequence':new['sequence'],'prepared_at':time.time(),'root':str(root.resolve()),'files':rows,'decision':'pending'}
    write(work/'plan.json',plan);return plan
def apply(root,work,data_dir):
    plan=read(work/'plan.json');envelope=read(work/'envelope.json');new=verify(envelope,root)
    if plan['decision']!='accept' or plan['root']!=str(root.resolve()) or plan['sequence']!=new['sequence']:raise ValueError('leader_acceptance_required')
    baseline=verify(read(root/'config/release-baseline.json'),root)
    if new['sequence']<=baseline['sequence']:raise ValueError('stale_candidate')
    expected=set(baseline['files'])|set(new['files'])
    if len(plan['files'])!=len(expected) or {r['path'] for r in plan['files']}!=expected:raise ValueError('invalid_plan_file_set')
    changes=[]
    for r in plan['files']:
        path=safe(root,r['path'])
        if r['choice'] not in ('keep','take','merge'):raise ValueError('unresolved_file_choice')
        if r['local']!=hashed(path):raise ValueError('local_file_changed_since_review')
        if r['new']!=new['files'].get(r['path'],{}).get('sha256') or r['base']!=baseline['files'].get(r['path'],{}).get('sha256'):raise ValueError('plan_manifest_mismatch')
        if r['choice']=='keep':continue
        candidate=safe(work/('merged' if r['choice']=='merge' else 'candidate'),r['path'])
        target_hash=r.get('merged_sha256') if r['choice']=='merge' else r['new']
        if target_hash is not None and hashed(candidate)!=target_hash:raise ValueError('candidate_changed_since_review')
        if r['choice']=='merge' and not target_hash:raise ValueError('merged_digest_required')
        changes.append({**r,'applied':target_hash,'candidate':str(candidate)})
    if (work/'journal.json').exists():raise ValueError('journal_exists_reconcile_first')
    live_web=bool(changes) and all(r['path'].startswith('web/') for r in changes)
    lock=None if live_web else InstanceLock(data_dir)
    try:
        backup=work/'backup';backup.mkdir()
        shutil.copyfile(root/'config/release-baseline.json',backup/'baseline.json')
        for r in changes:
            target=safe(root,r['path']);saved=safe(backup,r['path'])
            if target.exists():saved.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(target,saved)
        journal={'state':'applying','changes':changes,'live_web':live_web,'version':new['version']};write(work/'journal.json',journal)
        try:
            for r in changes:
                target=safe(root,r['path'])
                if r['applied'] is None:target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True,exist_ok=True);temp=target.with_name(target.name+'.control-pending');shutil.copyfile(r['candidate'],temp);os.replace(temp,target)
            write(root/'config/release-baseline.json',envelope)
            journal['state']='applied';write(work/'journal.json',journal)
        except Exception:
            restore(root,work,force=True);raise
        return {'state':'applied','files':len(changes),'reload':'interface' if live_web else 'restart','rollback':str(work)}
    finally:
        if lock:lock.close()
def restore(root,work,force=False):
    journal=read(work/'journal.json')
    if journal['state'] not in ('applying','applied'):raise ValueError('no_active_update')
    for r in journal['changes']:
        current=hashed(safe(root,r['path']))
        if not force and current not in (r['applied'],r['local']):raise ValueError('post_update_local_changes_need_review')
        if r['local'] is not None and hashed(safe(work/'backup',r['path']))!=r['local']:raise ValueError('backup_digest_mismatch')
    verify(read(work/'backup/baseline.json'),root)
    for r in journal['changes']:
        target=safe(root,r['path']);saved=safe(work/'backup',r['path'])
        if r['local'] is None:target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(saved,target)
    shutil.copyfile(work/'backup/baseline.json',root/'config/release-baseline.json')
    journal['state']='rolled_back';write(work/'journal.json',journal);return {'state':'rolled_back'}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--data-dir',type=Path,default=shared_directory())
    s=p.add_subparsers(dest='command',required=True)
    q=s.add_parser('prepare');q.add_argument('--manifest',type=Path,required=True);q.add_argument('--archive',type=Path,required=True);q.add_argument('--work',type=Path,required=True)
    for name in ('apply','rollback'):s.add_parser(name).add_argument('--work',type=Path,required=True)
    a=p.parse_args()
    if a.command=='prepare':result=prepare(a.root,a.manifest,a.archive,a.work)
    elif a.command=='apply':result=apply(a.root,a.work,a.data_dir)
    else:
        lock=InstanceLock(a.data_dir)
        try:result=restore(a.root,a.work)
        finally:lock.close()
    print(json.dumps(result,ensure_ascii=False,indent=2))
