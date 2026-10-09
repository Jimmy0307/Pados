# SPDX-License-Identifier: AGPL-3.0-only
from collections import defaultdict, deque


def topological_sort(dependencies: dict[str, set[str]]) -> list[str]:
    nodes = set(dependencies)
    for upstream_nodes in dependencies.values():
        nodes.update(upstream_nodes)
    incoming = {node: 0 for node in nodes}
    outgoing: dict[str, set[str]] = defaultdict(set)
    for node, dependency_nodes in dependencies.items():
        incoming.setdefault(node, 0)
        for dependency_node in dependency_nodes:
            outgoing[dependency_node].add(node)
            incoming[node] += 1
    ready = deque(sorted(node for node, count in incoming.items() if count == 0))
    ordered: list[str] = []
    while ready:
        node = ready.popleft()
        ordered.append(node)
        for target in sorted(outgoing[node]):
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if len(ordered) != len(nodes):
        raise ValueError("dependency cycle detected")
    return ordered
