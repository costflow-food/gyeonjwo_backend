"""Server workflow: prepare new RFQs, dispatch only drafts allowed by the owner send settings."""
import logging
import threading
from app.core.errors import ServiceError
from app.core.logs import error_origin

logger = logging.getLogger('uvicorn.error.rfq_workflow')


class RfqWorkflowWorker:
    def __init__(self, repo, preparation, delivery, owners):
        # owners is called every cycle so newly connected mailboxes are picked up without a restart.
        self.repo, self.preparation, self.delivery = repo, preparation, delivery
        self.owners = owners
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.running = False
        self.send_cursors = {}
        self.prepare_cursors = {}

    def process_owner(self, owner):
        self.repo.recover_server(owner)
        # A bounded, owner-filtered work scan. The deterministic persisted agent
        # run ID prevents two servers from preparing the same batch/revision.
        query = (self.repo.client.table('work_cases').select('id,input_revision,current_batch_id')
            .eq('owner_user_id', owner).eq('status', 'Running')
            .in_('processing_step', ['SELECTING_CANDIDATES', 'CANDIDATES']).is_('archived_at', 'null')
            .order('id').limit(50))
        if self.prepare_cursors.get(owner):
            query = query.gt('id', self.prepare_cursors[owner])
        rows = self.repo.data(query)
        self.prepare_cursors[owner] = rows[-1]['id'] if len(rows) == 50 else None
        for row in rows:
            if self.stop.is_set():
                return
            try:
                # All send hosts check the durable batch marker, independently
                # of their local graph flag. This worker still dispatches drafts.
                if self.repo.graph_owns_case(owner, row['id'], row.get('current_batch_id')):
                    continue
                self.preparation.prepare(owner, row['id'], row['input_revision'])
                logger.info('rfq preparation_finished')
            except ServiceError as error:
                if error.code != 'WORK_ALREADY_STARTED':
                    logger.warning('rfq preparation_failed code=PREPARATION_UNAVAILABLE case_id=%s cause_code=%s %s',
                                   row['id'], error.code, error_origin(error))
        for job in self.repo.reconcile_jobs(owner):
            if self.stop.is_set():
                return
            try:
                self.delivery.reconcile(owner, job['draft_id'])
            except Exception as error:
                self.repo.finish_reconcile_job(owner, job, failed=True)
                logger.warning('rfq reconciliation_unavailable draft_id=%s %s', job['draft_id'], error_origin(error))
            else:
                self.repo.finish_reconcile_job(owner, job)
        if self.stop.is_set():
            return
        pending = self.repo.pending(owner, after=self.send_cursors.get(owner))
        self.send_cursors[owner] = pending[-1]['id'] if len(pending) == 50 else None
        for draft in pending:
            if self.stop.is_set():
                break
            style = draft.get('style_snapshot_jsonb') or {}
            if style.get('producer') not in {'rfq-v1', 'graph-review-v1'}:
                continue
            # ON-created drafts are not silently sent when policy changes to OFF.
            if not (style.get('auto_policy_eligible') is True
                    or (draft.get('approved_by') == owner and draft.get('approved_at'))):
                continue
            try:
                result = self.delivery.send(owner, draft['id'])
            except Exception as error:
                logger.warning('rfq dispatch_failed code=DISPATCH_UNAVAILABLE draft_id=%s cause_code=%s %s', draft['id'],
                               getattr(error, 'code', None), error_origin(error))
            else:
                if isinstance(result, dict) and not result.get('sent_now'):
                    logger.info('rfq dispatch_skipped draft_id=%s status=%s', draft['id'], result.get('status'))

    def start(self):
        def loop():
            self.running = True
            try:
                while not self.stop.is_set():
                    try:
                        owners = self.owners()
                    except Exception as error:
                        owners = ()
                        logger.warning('rfq workflow_unavailable code=STORAGE_UNAVAILABLE %s', error_origin(error))
                    for owner in owners:
                        if self.stop.is_set():
                            break
                        try:
                            self.process_owner(owner)
                        except Exception as error:
                            logger.warning('rfq workflow_unavailable code=STORAGE_UNAVAILABLE %s', error_origin(error))
                    self.wake.wait(15)
                    self.wake.clear()
            finally:
                self.running = False
        self.thread = threading.Thread(target=loop, name='rfq-workflow', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=25)
