"""The two engines share one nonce sequence without contacting an exchange."""
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time

from zeus.signing import SigningCoordinator


def test_separate_engine_coordinators_serialize_the_same_agent():
    submitted = []
    barrier = threading.Barrier(2)

    class Exchange:
        def order(self):
            submitted.append(int(time.time() * 1000))
            time.sleep(.003)
            return {'status': 'ok'}

    with tempfile.TemporaryDirectory() as root:
        engines = [SigningCoordinator(root, '0x' + '1' * 40).wrap(Exchange()) for _ in range(2)]

        def submit(engine):
            barrier.wait()
            for _ in range(5):
                assert engine.order() == {'status': 'ok'}

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(submit, engines))

    assert len(submitted) == 10
    assert all(b > a for a, b in zip(submitted, submitted[1:]))
