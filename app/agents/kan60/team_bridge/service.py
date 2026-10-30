"""Business guards and lossless subset mapping to the unmodified team models."""
from copy import deepcopy

from pydantic import ValidationError
from app.agents.schemas import Plan,CounterOfferTarget,PlanOut,CounterOfferOut,AgentRole,AgentNextAction as N
from app.agents.kan60.counter_offer.contract import digest
from app.agents.kan60.purchase_plans.contract import ReviewError,n,parse
from app.agents.kan60.purchase_plans.tools import build_plan_set,verify_handoff
from app.agents.kan60.purchase_plans.negotiation import calculate_offer,validate_offer,run_offer_with_model,verify_plan_cost
from .repository import BridgeError
from .team_calculations import verify_team_total


def wire(value):
    return value.model_dump(mode='json') if hasattr(value,'model_dump') else deepcopy(value)


class BridgeService:
    def __init__(self,repository,draft_model):
        self.repo=repository;self.draft_model=draft_model;self.trace=[]

    def context(self,scope):
        snapshot=self.repo.snapshot(scope);data=snapshot['data']
        if data.get('revision')!=scope.revision:raise BridgeError('INPUT_REVISION_MISMATCH')
        # Missing facts are not fabricated, even when team *In passes Pydantic.
        try:parse(data)
        except ReviewError as exc:raise BridgeError('INPUT_'+exc.issue['code']+':'+exc.issue['field_name']) from exc
        refs={data['incumbent']['source_ref'],data['fixed_commitment']['source_ref'],data['policy']['source_ref']}
        refs.update(q['source_ref'] for q in data['quotes']);refs.update(q['cost_source_ref'] for q in data['quotes'])
        refs.update(t['source_ref'] for q in data['quotes'] for t in q.get('price_tiers',[]))
        refs.update(q['tier_method_ref'] for q in data['quotes'] if q.get('price_tiers'))
        refs.update(q['pack_multiple_source_ref'] for q in data['quotes'] if 'pack_multiple' in q)
        if not refs<=set(snapshot['evidence']):raise BridgeError('SOURCE_EVIDENCE_MISSING')
        for q in data['quotes']:
            matches=[key for key,v in snapshot['quote_bindings'].items() if v=={
                'supplier_id':q['supplier_id'],'quote_id':q['quote_id'],'version':q['version'],'kind':'CURRENT'}]
            if len(matches)!=1:raise BridgeError('QUOTE_BINDING_MISMATCH')
        if sum(v.get('kind')=='CURRENT' for v in snapshot['quote_bindings'].values())!=len(data['quotes']):
            raise BridgeError('QUOTE_BINDING_MISMATCH')
        notice=snapshot.get('notice_quote_ids',[])
        if 'notice_basis' in snapshot:
            # Team ① stores both stated prices in one current quote's source mail.
            # Preserve that real version ID; do not invent a BEFORE quote version.
            basis=snapshot['notice_basis'];requested=snapshot['quote_bindings'].get(basis['quote_version_id'],{})
            if (notice != [basis['quote_version_id']] or requested.get('kind')!='CURRENT'
                    or requested.get('supplier_id')!=data['incumbent']['supplier_id']
                    or basis['previous_unit_price']!=data['incumbent']['old_unit_price']
                    or basis['requested_unit_price']!=data['incumbent']['requested_unit_price']
                    or not {basis['before_source_ref'],basis['requested_source_ref']}<=set(snapshot['evidence'])):
                raise BridgeError('NOTICE_BASIS_MISMATCH')
        else:
            if len(notice)!=2 or len(set(notice))!=2:raise BridgeError('NOTICE_BASIS_MISSING')
            before,requested=(snapshot['quote_bindings'].get(key,{}) for key in notice)
            if (before.get('kind')!='BEFORE' or requested.get('kind')!='CURRENT'
                    or before.get('supplier_id')!=data['incumbent']['supplier_id']
                    or requested.get('supplier_id')!=data['incumbent']['supplier_id']
                    or before.get('unit_price')!=data['incumbent']['old_unit_price']):raise BridgeError('NOTICE_BASIS_MISMATCH')
        if n(data['fixed_commitment']['quantity'])!=0:raise BridgeError('TEAM_FIXED_ALLOCATION_UNREPRESENTABLE')
        bundle=build_plan_set(data)
        if bundle['outcome']!='SUCCESS':raise BridgeError('PLAN_INPUT_NOT_READY')
        if any(p['status']=='NEEDS_INFO' for p in bundle['plans']):raise BridgeError('TEAM_PARTIAL_SCOPE_UNREPRESENTABLE')
        valid=[p for p in bundle['plans'] if p['status']=='VALID']
        for row in valid:
            try:verify_plan_cost(data,row)
            except ReviewError as exc:raise BridgeError(exc.issue['code']) from exc
            verify_team_total(row)
        if not any(p['type']=='KEEP' for p in valid):
            raise BridgeError('KEEP_NOT_VERIFIED')
        return snapshot,data,bundle,valid

    def _plan(self,snapshot,data,row):
        ids={ (v['quote_id'],v['version']):key for key,v in snapshot['quote_bindings'].items() if v['kind']=='CURRENT'}
        return Plan(plan_type=row['type'],allocations=[{'supplier_id':a['supplier_id'],
            'quote_version_id':ids[(a['quote_id'],a['quote_version'])],'quantity':a['quantity']} for a in row['allocations']],
            total_amount=row['total_cost'],conditions=[
                'TEMP_CONTRACT: fixed=0; quantity=TOTAL=CHANGEABLE',
                'REPRICING_SCOPE:'+(data['repricing_scope'] or 'NOT_APPLICABLE_NO_SPLIT'),
                'TAX_BASIS:'+data['terms']['tax_basis'],
                'PERIOD:'+data['period']['from']+'/'+data['period']['to'],
                'SCOPE: provided verified quotes and declared allocation grid only']+
                (['PRICE_TIERS: all-units at each supplier allocation; off-grid boundaries not searched; no global optimum claim']
                 if any(q.get('price_tiers') for q in data['quotes']) else []))

    def build_candidates(self,scope):
        snapshot,data,bundle,valid=self.context(scope)
        return {'plans':[wire(self._plan(snapshot,data,row)) for row in valid],
                'excluded_plans':[p for p in bundle['plans'] if p['status']!='VALID'],
                'business_validated':False,'send_authorized':False,
                **({'tier_search':bundle['tier_search']} if 'tier_search' in bundle else {})}

    def validate_plans(self,scope,plans,require_refs=False):
        snapshot,data,bundle,valid=self.context(scope)
        if not plans:raise BridgeError('EMPTY_PLANS')
        try:parsed=[Plan.model_validate(wire(p)) for p in plans]
        except ValidationError as exc:raise BridgeError('PLAN_SCHEMA_INVALID') from exc
        expected=[wire(self._plan(snapshot,data,row)) for row in valid]
        actual=[]
        for p in parsed:
            content=wire(p);ref=content.pop('calculation_ref');content['calculation_ref']=None
            if ref:
                record=self.repo.resolve(scope,'PLAN_CALCULATION',ref)
                if record['status']!='PASS' or record['plan']!=content:raise BridgeError('PLAN_REFERENCE_MISMATCH')
                stored=self.repo.resolve(scope,'PLAN_SET',record['bundle_ref'])
                verify_handoff(data,stored['bundle'])
            elif require_refs:raise BridgeError('PLAN_VALIDATION_REFERENCE_REQUIRED')
            actual.append(content)
        # Exact set equality also prevents dropping a verified alternative from repricing.
        if sorted(map(digest,actual))!=sorted(map(digest,expected)):raise BridgeError('PLAN_ALLOCATION_TOTAL_OR_SET_MISMATCH')
        bundle_ref=self.repo.put_if_current(scope,'PLAN_SET',{'status':'PASS','bundle':bundle})
        checked=[]
        for p,row in zip(expected,valid):
            ref=self.repo.put_if_current(scope,'PLAN_CALCULATION',{'status':'PASS','plan':p,'row':row,'bundle_ref':bundle_ref})
            checked.append(Plan.model_validate({**p,'calculation_ref':ref}))
        return checked,bundle_ref,bundle

    def calculate(self,scope,plans):
        checked,bundle_ref,bundle=self.validate_plans(scope,plans,require_refs=True)
        snapshot=self.repo.snapshot(scope);data=snapshot['data'];result=calculate_offer(data,bundle)
        if result['decision']=='NO_COUNTER_NEEDED':raise BridgeError('UNAGREED_NO_COUNTER_ROUTE')
        if result['decision']!='READY':raise BridgeError('COUNTER_OFFER_'+result['decision'])
        validation=validate_offer(data,bundle,result)
        if not validation['valid']:raise BridgeError('INDEPENDENT_PRICE_VALIDATION_FAILED')
        policy_ref=self.repo.put_if_current(scope,'POLICY',{'policy':data['policy']})
        target=CounterOfferTarget(supplier_id=data['incumbent']['supplier_id'],target_unit_price=result['offer']['target_unit_price'],
            conditions=['REPRICING_SCOPE:'+(data['repricing_scope'] or 'NOT_APPLICABLE_NO_SPLIT'),'TAX_BASIS:'+data['terms']['tax_basis'],
                        'PERIOD:'+data['period']['from']+'/'+data['period']['to'],
                        'SCOPE: all plans in alternative_plan_ref repriced; not market optimum'],
            basis_quote_version_ids=snapshot['notice_quote_ids'],alternative_plan_ref=bundle_ref,policy_ref=policy_ref)
        ref=self.repo.put_if_current(scope,'OFFER_CALCULATION',{'status':'CALCULATED','result':result,'target':wire(target),
                                                              'bundle_ref':bundle_ref})
        return {'decision':'READY','target':wire(target),'calculation_ref':ref,'send_authorized':False}

    def _verify_target(self,scope,target,calculation_ref):
        """Read-only business revalidation, independent of storage ID generation."""
        try:target=CounterOfferTarget.model_validate(wire(target))
        except ValidationError as exc:raise BridgeError('TARGET_SCHEMA_INVALID') from exc
        record=self.repo.resolve(scope,'OFFER_CALCULATION',calculation_ref)
        if wire(target)!=record['target']:raise BridgeError('TARGET_CALCULATION_MISMATCH')
        data=self.repo.snapshot(scope)['data']
        policy=self.repo.resolve(scope,'POLICY',target.policy_ref)
        if policy['policy']!=data['policy']:raise BridgeError('POLICY_REFERENCE_MISMATCH')
        bundle=self.repo.resolve(scope,'PLAN_SET',target.alternative_plan_ref)['bundle']
        if target.alternative_plan_ref!=record['bundle_ref']:raise BridgeError('ALTERNATIVE_REFERENCE_MISMATCH')
        checked=validate_offer(data,bundle,record['result'])
        if not checked['valid']:raise BridgeError('INDEPENDENT_PRICE_VALIDATION_FAILED')
        for row in record['result']['comparison']['plans_at_proposed_price']:verify_team_total(row)
        self.repo.snapshot(scope)
        return target,checked

    def validate_target(self,scope,target,calculation_ref):
        """Explicit validation creation; only this path writes PRICE_VALIDATION."""
        target,checked=self._verify_target(scope,target,calculation_ref)
        ref=self.repo.put_if_current(scope,'PRICE_VALIDATION',{'status':'PASS','target':wire(target),
                    'calculation_ref':calculation_ref,'validation':checked})
        return target.model_copy(update={'validation_ref':ref})

    def checked_target(self,scope,target):
        if target is None:raise BridgeError('TARGET_REQUIRED')
        target=CounterOfferTarget.model_validate(wire(target))
        if not target.validation_ref:raise BridgeError('PRICE_VALIDATION_REFERENCE_REQUIRED')
        check=self.repo.resolve(scope,'PRICE_VALIDATION',target.validation_ref)
        base=wire(target);base['validation_ref']=None
        if check['status']!='PASS' or check['target']!=base:raise BridgeError('VALIDATION_TARGET_MISMATCH')
        _,verified=self._verify_target(scope,base,check['calculation_ref'])
        if check.get('validation')!=verified:raise BridgeError('VALIDATION_RESULT_MISMATCH')
        return target

    def create_draft(self,scope,target):
        target=self.checked_target(scope,target)
        data=self.repo.snapshot(scope)['data']
        bundle=self.repo.resolve(scope,'PLAN_SET',target.alternative_plan_ref)['bundle']
        result=run_offer_with_model(data,bundle,self.draft_model)
        if result['draft_status']!='VALIDATED_FOR_REVIEW':raise BridgeError('DRAFT_NOT_VALIDATED')
        if result['offer']['target_unit_price']!=target.target_unit_price:raise BridgeError('DRAFT_TARGET_MISMATCH')
        ref=self.repo.put_if_current(scope,'DRAFT',{'target':wire(target),'draft':result['draft'],'send_authorized':False})
        return {'draft_ids':[ref],'send_authorized':False}

    def check_output(self,scope,role,output,*,for_save=False):
        # Plan proposals may be invalid: they must reach the real FIX-capable node.
        if role==AgentRole.RESPONSE_PLAN:
            output=PlanOut.model_validate(wire(output))
            if output.next_action==N.PLANS_PROPOSED:
                if for_save:self.validate_plans(scope,output.plans,require_refs=True)
            elif output.plans:raise BridgeError('NON_PROPOSAL_HAS_PLANS')
        elif role==AgentRole.COUNTER_OFFER:
            output=CounterOfferOut.model_validate(wire(output))
            if output.next_action==N.COUNTER_OFFER_SKIPPED:
                # The latest team schema permits this action, but this adapter
                # has no independent skip proof/card implementation yet.
                raise BridgeError('TEAM_SKIP_ROUTE_NOT_IMPLEMENTED')
            if output.next_action==N.COUNTER_OFFER_PROPOSED:
                self.checked_target(scope,output.target)
                if not output.draft_ids:raise BridgeError('VALIDATED_DRAFT_REQUIRED')
                for ref in output.draft_ids:
                    draft=self.repo.resolve(scope,'DRAFT',ref)
                    if draft['send_authorized'] is not False or draft['target']!=wire(output.target):raise BridgeError('DRAFT_REFERENCE_MISMATCH')
            elif output.target is not None or output.draft_ids:raise BridgeError('NON_PROPOSAL_HAS_TARGET_OR_DRAFT')
        else:raise BridgeError('ROLE_NOT_IMPLEMENTED')
        if output.next_action in (N.NEEDS_EVIDENCE,N.RECHECK_SOURCE) and not output.unknown_fields:
            raise BridgeError('UNRESOLVED_FIELDS_REQUIRED')
        self.repo.snapshot(scope)
        return output

    def save_result(self,scope,role,output):
        checked=self.check_output(scope,role,output,for_save=True)
        ref=self.repo.put_if_current(scope,'AGENT_RESULT',{'role':str(role),'output':wire(checked),'send_authorized':False})
        return {'result_id':ref,'latest':True,'send_authorized':False}
