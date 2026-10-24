from dataclasses import dataclass


@dataclass
class NodeAck:
    status: str
    detail: str | None = None
    log: str = ""

    def __str__(self) -> str:
        return self.log

    def __getitem__(self, item: str):
        return getattr(self, item)


class NodeState:
    def __init__(self, mode: str = "FREE_ACCESS", led: str = "status"):
        self.mode = mode
        self.led = led

    def apply(self, command: dict) -> NodeAck:
        cmd_type = command.get("type", "")
        params = command.get("params") or {}

        if cmd_type == "mode":
            new_mode = params.get("mode", "FREE_ACCESS")
            self.mode = new_mode
            if new_mode == "FREE_ACCESS":
                self.led = "status"
            elif "color" in params:
                self.led = params["color"]
            return NodeAck(status="OK", detail=None, log=f"mode={self.mode}")

        if cmd_type == "rgb":
            if self.mode == "FREE_ACCESS":
                return NodeAck(status="FAILED", detail="not reserved", log="rgb FAILED: not reserved")
            color = params.get("color")
            if color:
                self.led = color
            return NodeAck(status="OK", detail=None, log=f"led={self.led}")

        return NodeAck(status="OK", detail=None, log=f"{cmd_type} OK")
