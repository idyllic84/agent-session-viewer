"""Per-device FIFO delivery, receipt ACK and single acknowledged executor."""
from pathlib import Path
import tempfile
import unittest
from server.archive import CloudArchive
from server.contracts.protocol import Problem


class TaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'cloud.db';self.archive=CloudArchive(self.path)
        self.device=self.archive.add_device('queue device',control=True)['deviceId']

    def queued(self):
        with self.archive.db.get_connection() as conn:
            return [r[0] for r in conn.execute('SELECT job_id FROM dist_job_queue WHERE device=? ORDER BY sequence',(self.device,))]

    def acknowledge(self,worker='worker'):
        delivery=self.archive.next_job(self.device,worker)
        ack=self.archive.received(self.device,delivery['job']['id'],worker,delivery['deliveryToken'])
        return delivery,ack

    def test_get_never_dequeues_and_ack_is_atomic_idempotent(self):
        job=self.archive.enqueue(self.device,'one',[])
        delivery=self.archive.next_job(self.device,'worker')
        self.assertEqual(self.queued(),[job['id']])
        self.assertEqual(delivery,self.archive.next_job(self.device,'worker'))
        ack=self.archive.received(self.device,job['id'],'worker',delivery['deliveryToken'])
        self.assertEqual(self.queued(),[]);self.assertEqual(ack['job']['state'],'RUNNING')
        repeated=self.archive.received(self.device,job['id'],'worker',delivery['deliveryToken'])
        self.assertEqual(repeated['job']['worker'],'worker');self.assertEqual(self.queued(),[])
        with self.archive.db.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM dist_jobs').fetchone()[0],1)

    def test_fifo_and_do_not_offer_next_task_before_finish(self):
        jobs=[self.archive.enqueue(self.device,str(i),[]) for i in range(3)]
        delivery,_=self.acknowledge()
        self.assertEqual(delivery['job']['id'],jobs[0]['id'])
        self.assertEqual(self.queued(),[j['id'] for j in jobs[1:]])
        current=self.archive.next_job(self.device,'worker')
        self.assertTrue(current['resume']);self.assertEqual(current['job']['id'],jobs[0]['id'])
        self.assertIsNone(self.archive.next_job(self.device,'other-worker'))
        self.archive.finish(self.device,jobs[0]['id'],'worker',{}, {'failed':0})
        self.assertEqual(self.archive.next_job(self.device,'worker')['job']['id'],jobs[1]['id'])

    def test_enqueue_only_deduplicates_same_idempotency_key(self):
        first=self.archive.enqueue(self.device,'first',[])
        self.assertEqual(self.archive.enqueue(self.device,'first',[])['id'],first['id'])
        second=self.archive.enqueue(self.device,'second',[])
        self.assertNotEqual(first['id'],second['id']);self.assertEqual(len(self.queued()),2)

    def test_wrong_or_replaced_delivery_ack_does_not_dequeue(self):
        job=self.archive.enqueue(self.device,'one',[]);first=self.archive.next_job(self.device,'first')
        with self.assertRaises(Problem):self.archive.received(self.device,job['id'],'first','wrong')
        self.assertIsNone(self.archive.next_job(self.device,'second'))
        with self.archive.db.get_connection() as conn:conn.execute('UPDATE dist_jobs SET delivery_until=0')
        second=self.archive.next_job(self.device,'second')
        with self.assertRaises(Problem):self.archive.received(self.device,job['id'],'first',first['deliveryToken'])
        self.assertEqual(self.queued(),[job['id']])
        self.archive.received(self.device,job['id'],'second',second['deliveryToken'])
        self.assertFalse(self.queued())

    def test_cancel_queued_task_removes_queue_only_and_retains_task_history(self):
        job=self.archive.enqueue(self.device,'cancel',[]);delivery=self.archive.next_job(self.device,'worker')
        self.archive.control(self.device,job['id'],'cancel')
        self.assertFalse(self.queued())
        with self.assertRaises(Problem):self.archive.received(self.device,job['id'],'worker',delivery['deliveryToken'])
        with self.archive.db.get_connection() as conn:self.assertEqual(conn.execute('SELECT state FROM dist_jobs').fetchone()[0],'CANCELLED')

    def test_no_heartbeat_expiry_for_assigned_worker_and_takeover_fences_it(self):
        job=self.archive.enqueue(self.device,'one',[]);self.acknowledge('old')
        with self.archive.db.get_connection() as conn:conn.execute('UPDATE dist_jobs SET lease_until=0')
        # No one took over: the acknowledged worker can still work after a long
        # CPU/file read with no heartbeat. A clock alone never invalidates it.
        with self.archive.db.get_connection() as conn:self.archive.check_lease(conn,self.device,job['id'],'old')
        resume=self.archive.next_job(self.device,'new');self.assertEqual(resume['job']['id'],job['id'])
        self.archive.received(self.device,job['id'],'new',resume['deliveryToken'])
        with self.archive.db.get_connection() as conn:
            with self.assertRaises(Problem):self.archive.check_lease(conn,self.device,job['id'],'old')
        self.assertFalse(self.queued())

    def test_business_activity_prevents_a_stale_resume_offer_stealing_ownership(self):
        job=self.archive.enqueue(self.device,'one',[]);self.acknowledge('old')
        with self.archive.db.get_connection() as conn:conn.execute('UPDATE dist_jobs SET lease_until=0')
        resume=self.archive.next_job(self.device,'new')
        self.archive.contact(self.device,job['id'],'old')
        with self.assertRaises(Problem):self.archive.received(self.device,job['id'],'new',resume['deliveryToken'])

    def test_restart_preserves_delivered_but_unacknowledged_and_acknowledged_jobs(self):
        job=self.archive.enqueue(self.device,'restart',[]);delivery=self.archive.next_job(self.device,'worker')
        again=CloudArchive(self.path)
        self.assertEqual(again.next_job(self.device,'worker')['deliveryToken'],delivery['deliveryToken'])
        self.assertEqual(self.queued(),[job['id']])
        again.received(self.device,job['id'],'worker',delivery['deliveryToken'])
        third=CloudArchive(self.path)
        self.assertFalse(self.queued());self.assertTrue(third.next_job(self.device,'worker')['resume'])

    def test_finish_is_idempotent_and_history_remains(self):
        job=self.archive.enqueue(self.device,'done',[]);delivery,_=self.acknowledge()
        result={'failed':0,'initialized':1}
        self.assertEqual(self.archive.finish(self.device,job['id'],'worker',{},result)['state'],'SUCCEEDED')
        self.assertEqual(self.archive.finish(self.device,job['id'],'worker',{},result)['state'],'SUCCEEDED')
        ack=self.archive.received(self.device,job['id'],'worker',delivery['deliveryToken'])
        self.assertTrue(ack['terminal']);self.assertIsNone(self.archive.next_job(self.device,'worker'))
        with self.assertRaises(Problem):self.archive.finish(self.device,job['id'],'worker',{}, {'failed':1})

    def test_new_pending_jobs_do_not_silently_expire_behind_long_running_task(self):
        first=self.archive.enqueue(self.device,'long',[]);self.acknowledge()
        second=self.archive.enqueue(self.device,'pending',[])
        with self.archive.db.get_connection() as conn:
            conn.execute('UPDATE dist_jobs SET created=1,expires=1 WHERE id=?',(second['id'],))
            self.archive.expire_jobs(conn)
        self.assertEqual(self.queued(),[second['id']])
        self.archive.finish(self.device,first['id'],'worker',{}, {'failed':0})
        self.assertEqual(self.archive.next_job(self.device,'worker')['job']['id'],second['id'])

    def test_additive_upgrade_backfills_only_pending_jobs(self):
        first=self.archive.enqueue(self.device,'active',[]);self.acknowledge()
        second=self.archive.enqueue(self.device,'pending',[])
        with self.archive.db.get_connection() as conn:
            conn.execute('DROP TABLE dist_job_queue')
            for column in ('delivery_worker','delivery_token','delivery_until','received_at'):
                conn.execute(f'ALTER TABLE dist_jobs DROP COLUMN {column}')
        upgraded=CloudArchive(self.path)
        self.assertEqual(self.queued(),[second['id']])
        with upgraded.db.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT state FROM dist_jobs WHERE id=?',(first['id'],)).fetchone()[0],'RUNNING')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM dist_devices').fetchone()[0],1)

    def test_device_queues_are_isolated(self):
        self.archive.enqueue(self.device,'own',[])
        other=self.archive.add_device('other',control=True)['deviceId']
        self.assertIsNone(self.archive.next_job(other,'worker'))
        job=self.archive.next_job(self.device,'worker')['job']
        with self.assertRaises(Problem):self.archive.received(other,job['id'],'worker','token')


if __name__=='__main__':unittest.main()
