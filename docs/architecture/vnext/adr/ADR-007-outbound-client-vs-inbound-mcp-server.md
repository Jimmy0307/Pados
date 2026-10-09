# ADR-007: Outbound MCP Client versus Inbound PADOS MCP Server

Status: `PROPOSED`

## Context
MCP has two different trust directions: PADOS calls tools, and external clients call PADOS tools.

## Decision
Use separate outbound Gateway and inbound Server boundaries with shared authentication, scopes, audit and failure rules.

## Alternatives Considered
One undifferentiated MCP integration or direct MCP-to-database access.

## Consequences
Inbound tools map to Harness application commands; outbound calls remain execution-scoped.

## Risks
Tool schemas can become a privilege-escalation surface.

## Migration Impact
No server is created now; both sides require independent threat review.

## Validation Criteria
Scope, rate-limit, denial, transport and direct-write prohibition tests pass.
