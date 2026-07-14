import threading
import time
import unittest

from tomo_core.typing_status import TypingLease


class TypingLeaseTests(unittest.TestCase):
    def test_start_pulses_immediately_and_refreshes_after_controlled_wake(self):
        pulses = []
        wake = threading.Event()
        entered_wait = threading.Event()
        refreshed = threading.Event()

        def send_typing(actor_id):
            pulses.append(actor_id)
            if len(pulses) == 2:
                refreshed.set()

        def wait(closed, _interval):
            entered_wait.set()
            wake.wait()
            wake.clear()
            return closed.is_set()

        lease = TypingLease(send_typing, "chat", wait=wait)
        try:
            lease.start()
            self.assertEqual(pulses, ["chat"])
            self.assertTrue(entered_wait.wait(0.5))
            wake.set()
            self.assertTrue(refreshed.wait(0.5))
            self.assertEqual(pulses, ["chat", "chat"])
        finally:
            wake.set()
            lease.close()

    def test_pause_fences_pulses_and_failed_first_delivery_resumes_when_active(self):
        pulses = []
        release = threading.Event()
        started = threading.Event()

        def send_typing(_actor_id):
            pulses.append("pulse")
            started.set()
            release.wait()

        lease = TypingLease(send_typing, "chat", wait=lambda closed, _: closed.wait())
        starter = threading.Thread(target=lease.start)
        starter.start()
        self.assertTrue(started.wait(0.5))
        pauser = threading.Thread(target=lease.pause_for_first_delivery)
        pauser.start()
        self.assertTrue(pauser.is_alive())
        release.set()
        starter.join(0.5)
        pauser.join(0.5)
        self.assertFalse(pauser.is_alive())
        self.assertEqual(pulses, ["pulse"])

        lease.resume_after_failed_delivery()
        self.assertEqual(pulses, ["pulse", "pulse"])
        lease.close()

    def test_close_is_idempotent_and_prevents_post_close_pulses(self):
        pulses = []
        entered_wait = threading.Event()

        def wait(closed, _interval):
            entered_wait.set()
            return closed.wait()

        lease = TypingLease(pulses.append, "chat", wait=wait)
        lease.start()
        self.assertTrue(entered_wait.wait(0.5))
        lease.close()
        lease.close()
        self.assertEqual(pulses, ["chat"])
        self.assertFalse(lease.is_alive())

    def test_close_is_bounded_when_a_typing_request_is_stuck(self):
        started = threading.Event()
        release = threading.Event()

        def send_typing(_actor_id):
            started.set()
            release.wait()

        lease = TypingLease(
            send_typing,
            "chat",
            shutdown_timeout_seconds=0.02,
            wait=lambda closed, _: closed.wait(),
        )
        starter = threading.Thread(target=lease.start)
        starter.start()
        self.assertTrue(started.wait(0.5))

        before = time.monotonic()
        lease.close()
        self.assertLess(time.monotonic() - before, 0.2)

        release.set()
        starter.join(0.5)
        self.assertFalse(starter.is_alive())
        self.assertFalse(lease.is_alive())

    def test_typing_failure_is_swallowed_and_stops_the_lease(self):
        def fail(_actor_id):
            raise RuntimeError("unavailable")

        lease = TypingLease(fail, "chat", wait=lambda closed, _: closed.wait())
        lease.start()

        self.assertFalse(lease.is_alive())
        lease.close()

    def test_resume_does_not_pulse_when_generation_is_inactive(self):
        active = [True]
        pulses = []
        lease = TypingLease(pulses.append, "chat", is_active=lambda: active[0], wait=lambda closed, _: closed.wait())
        lease.start()
        lease.pause_for_first_delivery()
        active[0] = False

        lease.resume_after_failed_delivery()

        self.assertEqual(pulses, ["chat"])
        lease.close()


if __name__ == "__main__":
    unittest.main()
