"""Fresh approval step-up and governed ResultCandidate acceptance records.

Adds server-bound remote sessions, single-use approval step-up challenges /
proofs, persisted verified ResultCandidates and their single terminal human
decision. Semantic state is unaffected: acceptance still commits only through
the proof database's existing CAS (state_proof_commits).

Revision ID: 20261007_0017
Revises: 20261006_0016
Create Date: 2026-10-07
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20261007_0017"
down_revision: str | None = "20261006_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE remote_step_up_sessions (
            session_id text PRIMARY KEY,
            handle_hash text NOT NULL UNIQUE,
            actor_id text NOT NULL,
            issued_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            CHECK (session_id ~ '^rs-[0-9a-f]{32}$'),
            CHECK (handle_hash ~ '^[0-9a-f]{64}$'),
            CHECK (actor_id <> ''),
            CHECK (expires_at > issued_at)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE approval_step_up_challenges (
            challenge_id uuid PRIMARY KEY,
            nonce text NOT NULL UNIQUE,
            actor_id text NOT NULL,
            session_id text NOT NULL REFERENCES remote_step_up_sessions(session_id) ON DELETE RESTRICT,
            approval_ref text NOT NULL,
            kind text NOT NULL,
            project_id text NOT NULL,
            task_id text NOT NULL,
            goal_id text NULL,
            candidate_id text NOT NULL,
            candidate_digest text NOT NULL,
            action text NOT NULL,
            classification text NOT NULL,
            expected_scope_version integer NOT NULL,
            target_run_id text NOT NULL,
            binding_digest text NOT NULL,
            protocol_version text NOT NULL,
            schema_version text NOT NULL,
            issued_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            status text NOT NULL,
            verified_at timestamptz NULL,
            presence_method text NULL,
            credential_id text NULL,
            sign_count bigint NULL,
            proof_id uuid NULL UNIQUE,
            proof_expires_at timestamptz NULL,
            proof_consumed_at timestamptz NULL,
            assertion_client_data text NULL,
            assertion_authenticator_data text NULL,
            assertion_signature text NULL,
            assertion_digest text NULL,
            CHECK (length(nonce) BETWEEN 40 AND 64),
            CHECK (kind IN ('PRODUCT_RUN', 'RESULT_CANDIDATE')),
            CHECK (action IN ('APPROVE', 'REJECT')),
            CHECK (classification IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')),
            CHECK (expected_scope_version >= 0),
            CHECK (binding_digest ~ '^[0-9a-f]{64}$'),
            CHECK (protocol_version ~ '^[0-9]+[.][0-9]+$'),
            CHECK (schema_version = 'pados.approval-step-up.v1'),
            CHECK (expires_at > issued_at),
            CHECK (status IN ('ISSUED', 'VERIFIED', 'CONSUMED', 'FAILED')),
            CHECK ((status IN ('VERIFIED', 'CONSUMED')) = (proof_id IS NOT NULL)),
            CHECK (proof_id IS NULL OR (proof_expires_at IS NOT NULL AND verified_at IS NOT NULL
                                        AND presence_method IS NOT NULL AND credential_id IS NOT NULL
                                        AND assertion_client_data IS NOT NULL
                                        AND assertion_authenticator_data IS NOT NULL
                                        AND assertion_signature IS NOT NULL
                                        AND assertion_digest ~ '^[0-9a-f]{64}$')),
            CHECK ((status = 'CONSUMED') = (proof_consumed_at IS NOT NULL))
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_approval_step_up_transition()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'approval step-up evidence is append-only';
            END IF;
            IF (NEW.challenge_id, NEW.nonce, NEW.actor_id, NEW.session_id, NEW.approval_ref, NEW.kind,
                NEW.project_id, NEW.task_id, NEW.goal_id, NEW.candidate_id, NEW.candidate_digest,
                NEW.action, NEW.classification, NEW.expected_scope_version, NEW.target_run_id,
                NEW.binding_digest, NEW.protocol_version, NEW.schema_version, NEW.issued_at, NEW.expires_at)
               IS DISTINCT FROM
               (OLD.challenge_id, OLD.nonce, OLD.actor_id, OLD.session_id, OLD.approval_ref, OLD.kind,
                OLD.project_id, OLD.task_id, OLD.goal_id, OLD.candidate_id, OLD.candidate_digest,
                OLD.action, OLD.classification, OLD.expected_scope_version, OLD.target_run_id,
                OLD.binding_digest, OLD.protocol_version, OLD.schema_version, OLD.issued_at, OLD.expires_at) THEN
                RAISE EXCEPTION 'approval step-up binding is immutable';
            END IF;
            IF NOT ((OLD.status = 'ISSUED' AND NEW.status IN ('VERIFIED', 'FAILED'))
                    OR (OLD.status = 'VERIFIED' AND NEW.status = 'CONSUMED')) THEN
                RAISE EXCEPTION 'approval step-up transition % -> % refused', OLD.status, NEW.status;
            END IF;
            IF OLD.status = 'VERIFIED' AND (NEW.proof_id, NEW.proof_expires_at, NEW.verified_at,
                    NEW.presence_method, NEW.credential_id, NEW.sign_count, NEW.assertion_client_data,
                    NEW.assertion_authenticator_data, NEW.assertion_signature, NEW.assertion_digest)
                IS DISTINCT FROM (OLD.proof_id, OLD.proof_expires_at, OLD.verified_at,
                    OLD.presence_method, OLD.credential_id, OLD.sign_count, OLD.assertion_client_data,
                    OLD.assertion_authenticator_data, OLD.assertion_signature, OLD.assertion_digest) THEN
                RAISE EXCEPTION 'approval step-up proof evidence is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_approval_step_up_challenges_transition
        BEFORE UPDATE OR DELETE ON approval_step_up_challenges
        FOR EACH ROW EXECUTE FUNCTION enforce_approval_step_up_transition()
        """
    )
    op.execute(
        "CREATE INDEX ix_approval_step_up_credential ON approval_step_up_challenges(credential_id)"
    )
    op.execute(
        "CREATE INDEX ix_approval_step_up_actor_status ON approval_step_up_challenges(actor_id, status)"
    )
    op.execute(
        """
        CREATE TABLE result_acceptance_candidates (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            candidate_id text NOT NULL UNIQUE,
            candidate_digest text NOT NULL,
            task_id text NOT NULL,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
            request_id uuid NOT NULL REFERENCES control_work_requests(id) ON DELETE RESTRICT,
            loop_id uuid NOT NULL REFERENCES task_loops(id) ON DELETE RESTRICT,
            plan_id uuid NOT NULL REFERENCES task_plans(id) ON DELETE RESTRICT,
            classification text NOT NULL,
            expected_scope_version integer NOT NULL,
            candidate_json jsonb NOT NULL,
            verification_json jsonb NOT NULL,
            verification_digest text NOT NULL,
            verification_status text NOT NULL,
            approval_id uuid NULL UNIQUE REFERENCES control_approvals(id) ON DELETE RESTRICT,
            protocol_version text NOT NULL,
            schema_version text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
            CHECK (verification_digest ~ '^[0-9a-f]{64}$'),
            CHECK (classification IN ('PUBLIC', 'INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')),
            CHECK (expected_scope_version >= 0),
            CHECK (verification_status IN ('PASSED', 'FAILED')),
            CHECK ((verification_status = 'PASSED') = (approval_id IS NOT NULL)),
            CHECK (protocol_version ~ '^[0-9]+[.][0-9]+$'),
            CHECK (schema_version = 'pados.result-acceptance-candidate.v1')
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_result_acceptance_candidates_task ON result_acceptance_candidates(task_id, loop_id)"
    )
    op.execute(
        """
        CREATE TABLE result_acceptance_decisions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            candidate_id text NOT NULL UNIQUE
                REFERENCES result_acceptance_candidates(candidate_id) ON DELETE RESTRICT,
            decision text NOT NULL,
            actor_id text NOT NULL,
            session_id text NOT NULL,
            proof_id uuid NOT NULL UNIQUE
                REFERENCES approval_step_up_challenges(proof_id) ON DELETE RESTRICT,
            binding_hash text NOT NULL,
            step_up_binding_digest text NOT NULL,
            next_action text NULL,
            decided_at timestamptz NOT NULL DEFAULT now(),
            CHECK (decision IN ('APPROVE', 'REJECT')),
            CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
            CHECK (step_up_binding_digest ~ '^[0-9a-f]{64}$'),
            CHECK (next_action IS NULL OR next_action IN ('REQUEST_REPLAN'))
        )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_result_acceptance_evidence_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'result acceptance evidence is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("result_acceptance_candidates", "result_acceptance_decisions"):
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only_{operation.lower()}
                BEFORE {operation} ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_result_acceptance_evidence_mutation()
                """
            )
    for table in ("result_acceptance_candidates", "result_acceptance_decisions", "approval_step_up_challenges"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_truncate
            BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION reject_result_acceptance_evidence_mutation()
            """
        )


def downgrade() -> None:
    for table in ("result_acceptance_candidates", "result_acceptance_decisions", "approval_step_up_challenges"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON {table}")
    op.execute("DROP TRIGGER IF EXISTS trg_approval_step_up_challenges_transition ON approval_step_up_challenges")
    op.execute("DROP FUNCTION IF EXISTS enforce_approval_step_up_transition()")
    for table in ("result_acceptance_decisions", "result_acceptance_candidates"):
        for operation in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_{operation} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_result_acceptance_evidence_mutation()")
    op.execute("DROP TABLE IF EXISTS result_acceptance_decisions")
    op.execute("DROP INDEX IF EXISTS ix_result_acceptance_candidates_task")
    op.execute("DROP TABLE IF EXISTS result_acceptance_candidates")
    op.execute("DROP INDEX IF EXISTS ix_approval_step_up_actor_status")
    op.execute("DROP INDEX IF EXISTS ix_approval_step_up_credential")
    op.execute("DROP TABLE IF EXISTS approval_step_up_challenges")
    op.execute("DROP TABLE IF EXISTS remote_step_up_sessions")
