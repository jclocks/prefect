import datetime
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Union

import dask
import dask.bag

import prefect
from prefect.core.edge import Edge

if TYPE_CHECKING:
    import prefect.engine.runner
    import prefect.engine.state
    from prefect.engine.state import State
StateList = Union["State", List["State"]]


class Heartbeat:
    def __init__(self, interval: int, function: Callable) -> None:
        self.interval = interval
        self.rate = min(interval, 1)
        self.function = function
        self._exit = False

    def start(self) -> None:
        def looper() -> None:
            iters = 0
            while not self._exit:
                if round(iters % self.rate) == 0:
                    self.function()
                iters = (iters + 1) % self.interval
                time.sleep(self.rate)

        self.executor = ThreadPoolExecutor(max_workers=2)
        self.fut = self.executor.submit(looper)

    def cancel(self) -> None:
        self._exit = True
        if hasattr(self, "executor"):
            self.executor.shutdown()


def run_with_heartbeat(
    runner_method: Callable[..., "prefect.engine.state.State"]
) -> Callable[..., "prefect.engine.state.State"]:
    """
    Utility decorator for running class methods with a heartbeat.  The class should implement
    `self._heartbeat` with no arguments.
    """

    @wraps(runner_method)
    def inner(
        self: "prefect.engine.runner.Runner", *args: Any, **kwargs: Any
    ) -> "prefect.engine.state.State":
        timer = Heartbeat(prefect.config.cloud.heartbeat_interval, self._heartbeat)
        try:
            try:
                if self._heartbeat():
                    timer.start()
            except Exception as exc:
                self.logger.exception(
                    "Heartbeat failed to start.  This could result in a zombie run."
                )
            return runner_method(self, *args, **kwargs)
        finally:
            timer.cancel()

    return inner


def timeout_handler(
    fn: Callable, *args: Any, timeout: int = None, **kwargs: Any
) -> Any:
    """
    Helper function for implementing timeouts on function executions.
    Implemented via `concurrent.futures.ThreadPoolExecutor`.

    Args:
        - fn (callable): the function to execute
        - *args (Any): arguments to pass to the function
        - timeout (int): the length of time to allow for
            execution before raising a `TimeoutError`, represented as an integer in seconds
        - **kwargs (Any): keyword arguments to pass to the function

    Returns:
        - the result of `f(*args, **kwargs)`

    Raises:
        - TimeoutError: if function execution exceeds the allowed timeout
    """
    if timeout is None:
        return fn(*args, **kwargs)

    executor = ThreadPoolExecutor()

    def run_with_ctx(*args: Any, _ctx_dict: dict, **kwargs: Any) -> Any:
        with prefect.context(_ctx_dict):
            return fn(*args, **kwargs)

    fut = executor.submit(
        run_with_ctx, *args, _ctx_dict=prefect.context.to_dict(), **kwargs
    )

    try:
        return fut.result(timeout=timeout)
    except FutureTimeout:
        raise TimeoutError("Execution timed out.")
