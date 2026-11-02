"""Credential-redacted diagnostics; never capture stack locals or HTTP headers."""
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def safe_log(value):
    if isinstance(value, str):
        value = re.sub(r'sk-[A-Za-z0-9_-]{16,}', '[REDACTED_KEY]', value)
        return re.sub(r'(?i)Bearer\s+[^\s"\']+', 'Bearer [REDACTED]', value)
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if str(k).lower() in {'api_key', 'authorization', 'access_token', 'secret'} else safe_log(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_log(v) for v in value]
    return value


class ResponseValidationError(ValueError):
    def __init__(self, field, expected, actual, message):
        super().__init__(message)
        self.details = {'field': field, 'expected': expected, 'actual': actual}


def validate_monitor(result):
    def fail(field, expected, actual, message):
        raise ResponseValidationError(field, expected, actual, message)
    if not isinstance(result, dict):
        fail('response', 'object', type(result).__name__, '감시 응답이 객체가 아닙니다.')
    decision, items, checks = result.get('decision'), result.get('requests'), result.get('checks')
    if decision not in ('continue', 'verify', 'human'):
        fail('decision', ['continue','verify','human'], decision, '감시 decision 값이 허용 목록에 없습니다.')
    if not isinstance(result.get('reason'), str):
        fail('reason', 'string', type(result.get('reason')).__name__, '감시 reason은 문자열이어야 합니다.')
    if not isinstance(items, list):
        fail('requests', 'array', type(items).__name__, '감시 requests는 목록이어야 합니다.')
    if len(items) > 6:
        fail('requests.length', '<= 6', len(items), '감시 질문 %s개: 응답당 최대 6개를 초과했습니다.' % len(items))
    if not isinstance(checks, list):
        fail('checks', 'array', type(checks).__name__, '감시 checks는 목록이어야 합니다.')
    if (decision == 'human') != bool(items):
        fail('decision/requests', 'human일 때만 질문 1개 이상', {'decision':decision,'requests_count':len(items)}, '감시 판단과 질문 유무가 일치하지 않습니다.')
    if decision == 'verify' and not checks:
        fail('checks.length', '>= 1 when verify', 0, 'verify 판단에 검증 항목이 없습니다.')
    for index, item in enumerate(items):
        prefix = 'requests[%s]' % index
        if not isinstance(item, dict):
            fail(prefix, 'object', type(item).__name__, '%s는 객체여야 합니다.' % prefix)
        key = item.get('key')
        if not isinstance(key, str) or not re.fullmatch('[a-z0-9_]{1,64}', key):
            fail(prefix+'.key', '[a-z0-9_]{1,64}', key, '%s.key 형식이 잘못됐습니다.' % prefix)
        for field in ['question','reason']:
            value=item.get(field)
            if not isinstance(value, str) or not 1 <= len(value) <= 2000:
                fail(prefix+'.'+field, 'string length 1..2000', {'type':type(value).__name__,'length':len(value) if isinstance(value,str) else None}, '%s.%s는 1~2000자 문자열이어야 합니다.' % (prefix,field))
        affected=item.get('affected_tasks')
        if not isinstance(affected,list) or not affected or any(not isinstance(x,str) or x not in {'input','policy','docs'} for x in affected):
            fail(prefix+'.affected_tasks', 'nonempty array of input/policy/docs', affected, '%s.affected_tasks가 허용 작업 목록과 다릅니다.' % prefix)
        if item.get('priority') not in ('normal','high'):
            fail(prefix+'.priority', ['normal','high'], item.get('priority'), '%s.priority 값이 허용 목록에 없습니다.' % prefix)


def error_record(exc, call, phase, code_version, epoch):
    envelope = call.get('input', {}) if call else {}
    record = {'at':timestamp(), 'agent':call.get('agent','system') if call else 'system',
              'call_id':call.get('id') if call else None, 'model':envelope.get('model'),
              'action':envelope.get('action'), 'stage':call.get('stage','workflow') if call else 'workflow',
              'phase':phase, 'code_version':code_version, 'execution_epoch':epoch,
              'exception_type':type(exc).__name__, 'message':str(exc),
              'details':getattr(exc,'details',{}), 'response_id':call.get('response_id') if call else None,
              'elapsed_ms':call.get('elapsed_ms') if call else None,
              'trace': [{'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
                        for f in traceback.extract_tb(exc.__traceback__)[-8:]]}
    return safe_log(record)
