"""Role routing for the existing Pro graph."""

from artemis.graph.graph import build_pro_graph, build_pro_nodes


class RoleGraphExtension:
    def __init__(self, runtime):
        self.runtime = runtime
        self.next_turn = "role_route"
        self.execution_edge = runtime.execution_edge

    def wrap_node(self, name, node):
        return self.runtime.wrap_node(name, node)

    def add_turn_boundary(self, builder):
        builder.add_node("role_route", self.runtime.begin_turn)
        builder.add_node("role_boundary", self.runtime.boundary)
        builder.add_edge("role_route", "perception")
        builder.add_edge("operator", "role_boundary")
        builder.add_edge("role_boundary", "execution_check")


async def get_multi_device_graph(runtime):
    runtime.nodes = {
        role: build_pro_nodes(ctx) for role, ctx in runtime.contexts.items()
    }
    return await build_pro_graph(
        runtime.control_ctx,
        extension=RoleGraphExtension(runtime),
        nodes=runtime.nodes[runtime.control_ctx.role_id],
    )
