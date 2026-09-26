"""Bounded, redacted private diagnostic contract shared by client and cloud."""
import json,re

FIELDS={'category','severity','title','summary','steps','expected','actual','diagnostics','fix_summary','verification','variant_sha256'}
LIMITS={'title':160,'summary':1200,'steps':1000,'expected':500,'actual':1000,'diagnostics':1200,'fix_summary':800,'verification':600}
CATEGORIES={'bug','crash','performance','security','update','website'}
SEVERITIES={'low','normal','high','critical'}

class FeedbackError(Exception):
    def __init__(self,code,status=400):self.code,self.status=code,status

def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def redact(text):
    # Both ends repeat this filter. Selection/review remains mandatory: regex is
    # not a proof that arbitrary project material is safe to share.
    patterns=[
        (r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)','[private key removed]'),
        (r'(?im)\b(?:authorization|cookie|set-cookie|password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)\b\s*[:=]\s*[^\r\n]+','[credential removed]'),
        (r'(?i)\bBearer\s+[A-Za-z0-9_.~+/-]+=*','[credential removed]'),
        (r'\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{16,})','[credential removed]'),
        (r'(?i)\bhttps?://[^\s<>\"\']+','[URL removed]'),
        (r'(?i)\b[A-Z]:[\\/][^\r\n\"<>]*|(?:/Users/|/home/|/root/|/srv/|/opt/|/tmp/)[^\s\"<>]*|\\\\[^\s\"<>]+','[path removed]'),
        (r'\b[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b','[email removed]'),
        (r'\b(?:\d{1,3}\.){3}\d{1,3}\b','[IP removed]'),
        (r'(?<!\w)(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.]*(?:%[\w.-]+)?','[IP removed]'),
        (r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])','[opaque value removed]'),
    ]
    for pattern,replacement in patterns:text=re.sub(pattern,replacement,text)
    return ''.join(c for c in text if c in '\n\t' or ord(c)>=32).strip()

def prepare(report):
    if not isinstance(report,dict) or set(report)-FIELDS or not {'category','severity','title','summary'}<=set(report):
        raise FeedbackError('invalid_feedback_fields')
    if report['category'] not in CATEGORIES or report['severity'] not in SEVERITIES:raise FeedbackError('invalid_feedback_classification')
    result={'category':report['category'],'severity':report['severity']};changed=False
    for field,limit in LIMITS.items():
        if field not in report:continue
        value=report[field]
        if not isinstance(value,str) or len(value)>limit:raise FeedbackError('feedback_text_limit')
        safe=redact(value);changed|=safe!=value
        if field in ('title','summary') and not safe:raise FeedbackError('feedback_text_required')
        result[field]=safe
    if 'variant_sha256' in report:
        if not isinstance(report['variant_sha256'],str) or not re.fullmatch('[a-f0-9]{64}',report['variant_sha256']):raise FeedbackError('invalid_variant_digest')
        result['variant_sha256']=report['variant_sha256']
    if len(canonical(result).encode())>6000:raise FeedbackError('feedback_body_limit',413)
    return result,changed

