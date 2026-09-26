"""Shared validation for the coordination API."""
import re

class Fault(Exception):
 def __init__(self,code,status=400):self.code=code;self.status=status

def text(value,maximum=5000,empty=False):
 if not isinstance(value,str) or len(value)>maximum or (not empty and not value.strip()):raise Fault('invalid_text')
 if any(ord(c)<32 and c not in '\n\t\r' for c in value):raise Fault('control_characters')
 if re.search(r'-----BEGIN (?:\w+ )*PRIVATE KEY-----|\b(?:sk-|sk_|ghp_|github_pat_)[A-Za-z0-9_-]{16,}|\bBearer\s+[A-Za-z0-9_.-]{20,}|(?:密码|password|api[_ -]?key|secret)\s*[:=：]\s*["\']?[^\s"\']{6,}',value,re.I):raise Fault('possible_secret_use_reference_instead')
 return value.strip()

def ident(v):
 if not isinstance(v,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,100}',v):raise Fault('invalid_identifier')
 return v

def scope(v):
 v=text(v,240).replace('\\','/').strip('/').lower()
 if not v or ':' in v or any(x in ('','.','..') for x in v.split('/')):raise Fault('invalid_scope')
 return v

def overlaps(a,b):return a==b or a.startswith(b+'/') or b.startswith(a+'/') or a=='*' or b=='*'

