import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from sparks.emit import FLUSH_SECONDS, RunRecord


class Stalling(BaseHTTPRequestHandler):
    inflight = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()
    arrived = threading.Event()

    def do_POST(self) -> None:
        with Stalling.lock:
            Stalling.inflight += 1
            Stalling.peak = max(Stalling.peak, Stalling.inflight)
        Stalling.arrived.set()
        # Hold the request open the way an overloaded Prometheus does.
        Stalling.release.wait(timeout=30)
        with Stalling.lock:
            Stalling.inflight -= 1
        try:
            self.send_response(200)
            self.end_headers()
        except OSError:
            pass

    def log_message(self, *args: object) -> None:
        pass


def test_shutdown_never_puts_a_second_writer_on_the_wire() -> None:
    Stalling.inflight = Stalling.peak = 0
    Stalling.release.clear()
    Stalling.arrived.clear()
    server = HTTPServer(("127.0.0.1", 0), Stalling)
    # Threaded accept, so a second concurrent request would actually be served
    # and counted rather than queueing in the listen backlog and hiding the bug.
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    # A literal, not the module constant: the suite turns retries off, and
    # without them the pump gives up on the stalled receiver early enough to
    # be reaped, so close() takes its flushing path and never exercises the
    # wedged-pump branch this test exists for.
    m = RunRecord(
        run_id="run-stall",
        url=f"http://127.0.0.1:{port}",
        autostart=True,
        retries=3,
    )
    m.begin()

    # Wait for the pump to be inside a send() and stuck there. Waiting on the
    # request rather than sleeping a fixed span: under load the thread may not
    # be scheduled inside it, which is a flake, not a failure.
    assert Stalling.arrived.wait(timeout=FLUSH_SECONDS * 4), (
        "the pump never reached the server"
    )

    started = time.monotonic()
    m.end("finished")
    elapsed = time.monotonic() - started

    assert Stalling.peak == 1, (
        f"{Stalling.peak} concurrent requests; shutdown flushed while the pump "
        "was still sending"
    )
    # end() must not hang on a stalled receiver for the rest of the afternoon.
    assert elapsed < FLUSH_SECONDS * 2 + 5, f"end() blocked for {elapsed:.1f}s"

    Stalling.release.set()
    server.shutdown()
    server.server_close()
