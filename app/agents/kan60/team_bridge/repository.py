"""Reference storage boundary. Production implementations require atomic CAS writes."""
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Protocol

from app.agents.kan60.counter_offer.contract import digest


class BridgeError(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


@dataclass(frozen=True)
class Scope:
    case_id: str
    revision: int
    quote_ids: tuple[str,...]

    @classmethod
    def from_input(cls,data):
        ids=tuple(data.quote_version_ids)
        if not ids or len(ids)!=len(set(ids)):raise BridgeError('QUOTE_VERSION_IDS_REQUIRED')
        return cls(str(data.case_id),data.input_revision,tuple(sorted(ids)))


class EvidenceRepository(Protocol):
    """All methods enforce authenticated case ownership, revision and quote versions.

    put_if_current must check AND write in one transaction/CAS. resolve must bind
    kind, case, revision, quote IDs, source digest and integrity, not just ref existence.
    """
    def current_revision(self,case_id:str)->int: ...
    def scope_for_case(self,case_id:str,revision:int)->Scope: ...
    def snapshot(self,scope:Scope)->dict: ...
    def resolve(self,scope:Scope,kind:str,ref:str)->dict: ...
    def put_if_current(self,scope:Scope,kind:str,payload:dict)->str: ...


class SyntheticRepository:
    """Explicit EVAL_ONLY in-memory test repository, NOT a real DB adapter."""
    def __init__(self,snapshots,authorized_case_ids):
        self._lock=RLock();self._cases=deepcopy(snapshots);self._records={}
        self.authorized=frozenset(map(str,authorized_case_ids));self.audit=[]
        for value in self._cases.values():
            if value.get('usage')!='EVAL_ONLY' or value['data'].get('data_kind')!='SYNTHETIC':
                raise ValueError('Synthetic repository accepts explicitly labelled evaluation data only')

    def _case(self,case):
        if case not in self.authorized or case not in self._cases:raise BridgeError('CASE_SCOPE_DENIED')
        return self._cases[case]

    def current_revision(self,case_id):
        with self._lock:return self._case(str(case_id))['revision']

    def scope_for_case(self,case_id,revision):
        """Resolve authenticated current quote IDs, never IDs supplied by a draft."""
        with self._lock:
            case=self._case(str(case_id))
            scope=Scope(str(case_id),revision,tuple(sorted(case['quote_bindings'])))
            self._check(scope)
            return scope

    def _check(self,scope):
        case=self._case(scope.case_id)
        if type(scope.revision) is not int or scope.revision!=case['revision']:raise BridgeError('STALE_REVISION')
        if scope.quote_ids!=tuple(sorted(case['quote_bindings'])):raise BridgeError('STALE_QUOTE_VERSIONS')
        return case

    def snapshot(self,scope):
        with self._lock:return deepcopy(self._check(scope))

    def put_if_current(self,scope,kind,payload):
        with self._lock:
            case=self._check(scope)
            record={'case_id':scope.case_id,'input_revision':scope.revision,'quote_version_ids':list(scope.quote_ids),
                    'snapshot_digest':digest(case),'kind':kind,'payload':deepcopy(payload)}
            ref='EVAL_ONLY:'+kind+':'+digest(record)
            self._records[ref]=record
            self.audit.append({'action':'write','kind':kind,'ref':ref,'revision':scope.revision})
            return ref

    def resolve(self,scope,kind,ref):
        with self._lock:
            case=self._check(scope);record=self._records.get(ref)
            if record is None:raise BridgeError('REFERENCE_NOT_FOUND')
            if (record['case_id']!=scope.case_id or record['input_revision']!=scope.revision
                    or record['quote_version_ids']!=list(scope.quote_ids) or record['kind']!=kind):
                raise BridgeError('REFERENCE_SCOPE_MISMATCH')
            if record['snapshot_digest']!=digest(case):raise BridgeError('REFERENCE_SOURCE_CHANGED')
            if ref!='EVAL_ONLY:'+kind+':'+digest(record):raise BridgeError('REFERENCE_TAMPERED')
            return deepcopy(record['payload'])

    def advance_revision(self,case_id):
        """Evaluation-only new evidence event. Existing records become unreadable."""
        with self._lock:
            case=self._case(str(case_id));case['revision']+=1;case['data']['revision']=case['revision']

