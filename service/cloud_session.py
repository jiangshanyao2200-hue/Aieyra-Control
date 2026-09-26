"""Account credentials use the operating system vault, separate from shared data."""
import ctypes,hashlib,json,os,subprocess,sys
from pathlib import Path

class SessionVault:
    def __init__(self,directory):
        self.directory=Path(directory);self.file=self.directory/'cloud-session.bin'
        self.account=hashlib.sha256(str(self.directory.resolve()).encode()).hexdigest()[:24]
    def _windows(self,raw,encrypt):
        class Blob(ctypes.Structure):
            _fields_=[('size',ctypes.c_ulong),('data',ctypes.POINTER(ctypes.c_ubyte))]
        buf=ctypes.create_string_buffer(raw);source=Blob(len(raw),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)));target=Blob()
        call=ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
        if not call(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target)):raise OSError('account_vault_unavailable')
        try:return ctypes.string_at(target.data,target.size)
        finally:ctypes.windll.kernel32.LocalFree(target.data)
    def load(self):
        try:
            if sys.platform=='win32':raw=self._windows(self.file.read_bytes(),False)
            elif sys.platform=='darwin':
                r=subprocess.run(['security','find-generic-password','-s','cn.aieyra.control','-a',self.account,'-w'],capture_output=True,timeout=5)
                if r.returncode:return None
                raw=r.stdout
            else:return None
            value=json.loads(raw)
            return value if isinstance(value,dict) and isinstance(value.get('access_token'),str) and isinstance(value.get('user'),dict) and isinstance(value.get('expires_at'),(int,float)) and value.get('scope')=='desktop' else None
        except (OSError,ValueError,subprocess.SubprocessError):return None
    def save(self,value):
        raw=json.dumps(value,separators=(',',':')).encode()
        try:
            if sys.platform=='win32':
                self.directory.mkdir(parents=True,exist_ok=True);pending=self.file.with_suffix('.pending')
                pending.write_bytes(self._windows(raw,True));os.replace(pending,self.file)
            elif sys.platform=='darwin':
                # macOS owns the encryption key; no account secret enters the shared database.
                r=subprocess.run(['security','add-generic-password','-U','-s','cn.aieyra.control','-a',self.account,'-w',raw.decode()],capture_output=True,timeout=5)
                return r.returncode==0
            else:return False
            return True
        except (OSError,subprocess.SubprocessError):return False
    def clear(self):
        try:self.file.unlink(missing_ok=True)
        except OSError:pass
        if sys.platform=='darwin':
            try:subprocess.run(['security','delete-generic-password','-s','cn.aieyra.control','-a',self.account],capture_output=True,timeout=5)
            except (OSError,subprocess.SubprocessError):pass
