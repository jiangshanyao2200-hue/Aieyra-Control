"""Seal native CI output to a release-specific operator public key."""
import argparse,hashlib,json,os,secrets,subprocess,tempfile,zipfile
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--key',type=Path,required=True);a=p.parse_args()
package=next(a.input.glob('aieyra-control-*.zip'));receipt=a.input/'build-receipt.json';expected=json.loads(receipt.read_text())
assert hashlib.sha256(package.read_bytes()).hexdigest()==expected['sha256']
with tempfile.TemporaryDirectory(prefix='control-sealed-') as t:
    temporary=Path(t);password=temporary/'password';password.write_text(secrets.token_hex(32));password.chmod(0o600)
    bundle=temporary/'bundle.zip'
    with zipfile.ZipFile(bundle,'w',zipfile.ZIP_STORED) as z:z.write(package,package.name);z.write(receipt,receipt.name)
    subprocess.run(['openssl','enc','-aes-256-cbc','-salt','-pbkdf2','-iter','200000','-in',str(bundle),'-out',str(a.input/'package.enc'),'-pass','file:'+str(password)],check=True)
    subprocess.run(['openssl','pkeyutl','-encrypt','-pubin','-inkey',str(a.key),'-in',str(password),'-out',str(a.input/'package.key'),'-pkeyopt','rsa_padding_mode:oaep','-pkeyopt','rsa_oaep_md:sha256'],check=True)
print('Sealed native artifact. Only the release operator can decrypt it.')
