class ToolGroup:
    def __init__(self, id):
        self.id = id
        self.states = []
        self.machines = {}


class State:
    def __init__(self, id):
        self.id = id
        self.machines = []


class Machine:
    def __init__(self, id):
        self.id = id


class Batch:
    pass


class Order:
    pass


class Step:
    pass

if __name__ == "__main__":
    batch_templates = []
    orders = []
    tool_groups = []