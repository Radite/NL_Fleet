"""
These tests are the ones I'd run in CI before ever trusting this tool with
a real DB credential — they exercise the validator against both benign
queries and known SQL-injection-style patterns.
"""

import pytest
from app.validator import validate_query


class TestValidQueries:
    def test_simple_select_with_limit(self):
        result = validate_query("SELECT hostname FROM devices LIMIT 10")
        assert result.ok

    def test_aggregate_without_limit_allowed(self):
        result = validate_query("SELECT COUNT(*) FROM devices")
        assert result.ok

    def test_join_across_allowed_tables(self):
        result = validate_query(
            "SELECT d.hostname, w.warranty_status FROM devices d "
            "JOIN warranty_lookup w ON d.asset_tag = w.asset_tag LIMIT 50"
        )
        assert result.ok

    def test_group_by_aggregate_needs_limit_or_is_bounded(self):
        # GROUP BY aggregates return one row per group, not one row total —
        # the validator currently requires LIMIT here since it can't bound
        # group cardinality. This test documents that behavior explicitly.
        result = validate_query("SELECT site, COUNT(*) FROM devices GROUP BY site")
        assert not result.ok
        result_with_limit = validate_query("SELECT site, COUNT(*) FROM devices GROUP BY site LIMIT 50")
        assert result_with_limit.ok

    def test_string_literal_containing_sql_keywords_does_not_confuse_validator(self):
        # A legitimate filter on a text column shouldn't be treated as SQL
        # syntax just because the *value* looks like SQL. This must still be
        # rejected — but for the right reason (no real LIMIT), not pass by
        # accident, and not be rejected for an unrelated wrong reason either.
        result = validate_query(
            "SELECT hostname FROM devices WHERE department = 'Engineering FROM legacy'"
        )
        assert not result.ok
        assert "LIMIT" in result.reason or "aggregate" in result.reason.lower()


class TestRejectedStatementTypes:
    @pytest.mark.parametrize("sql", [
        "DELETE FROM devices WHERE site = 'Provo'",
        "DROP TABLE devices",
        "UPDATE devices SET agent_status = 'healthy'",
        "INSERT INTO devices VALUES ('x')",
        "ALTER TABLE devices ADD COLUMN foo TEXT",
        "TRUNCATE TABLE devices",
        "CREATE TABLE evil (id INT)",
    ])
    def test_write_statements_rejected(self, sql):
        result = validate_query(sql)
        assert not result.ok

    def test_null_sql_rejected(self):
        result = validate_query(None)
        assert not result.ok

    def test_empty_string_rejected(self):
        result = validate_query("   ")
        assert not result.ok


class TestInjectionPatterns:
    def test_stacked_query_rejected(self):
        result = validate_query(
            "SELECT hostname FROM devices LIMIT 10; DROP TABLE devices;"
        )
        assert not result.ok

    def test_unknown_table_rejected(self):
        result = validate_query("SELECT * FROM users LIMIT 10")
        assert not result.ok

    def test_no_limit_no_aggregate_rejected(self):
        result = validate_query("SELECT * FROM devices")
        assert not result.ok

    def test_limit_over_max_rejected(self):
        result = validate_query("SELECT * FROM devices LIMIT 10000")
        assert not result.ok

    def test_comment_smuggled_ddl_still_caught_by_keyword_scan(self):
        # sqlparse will correctly identify this as a single SELECT statement,
        # but the forbidden-keyword scan should still catch the word DROP
        # appearing anywhere in the string, since a comment is not a
        # reliable place to hide instructions once the query is logged.
        result = validate_query("SELECT hostname FROM devices /* DROP TABLE devices */ LIMIT 10")
        assert not result.ok

    def test_limit_inside_string_literal_does_not_bypass_row_cap(self):
        # Regression test: a query with NO real LIMIT clause used to sneak
        # past the validator if a string literal happened to contain text
        # that matched the `LIMIT \d+` regex, e.g. a note field referencing
        # a ticket number. This must still be rejected for missing a real
        # LIMIT clause.
        result = validate_query(
            "SELECT * FROM devices WHERE hostname = 'see ticket LIMIT 999999 for details'"
        )
        assert not result.ok
        assert "LIMIT" in result.reason or "aggregate" in result.reason.lower()

    def test_fake_table_name_inside_string_literal_not_mistaken_for_real_table(self):
        # A value containing "FROM some_fake_table" should not itself get
        # treated as a real FROM clause target.
        result = validate_query(
            "SELECT hostname FROM devices WHERE department = 'Migrated FROM legacy_users' LIMIT 10"
        )
        assert result.ok  # only real referenced table is `devices`, which is allowed


class TestFewShotExamplesFromPrompt:
    """Sanity-check that the example queries in prompts.py actually pass
    the validator — if they didn't, the model would be trained on a
    contradiction between its few-shot examples and what's actually allowed."""

    def test_stale_checkin_example(self):
        from app.prompts import FEW_SHOT_EXAMPLES
        sql = FEW_SHOT_EXAMPLES[0]["answer"]["sql"]
        assert validate_query(sql).ok

    def test_warranty_groupby_example(self):
        from app.prompts import FEW_SHOT_EXAMPLES
        sql = FEW_SHOT_EXAMPLES[1]["answer"]["sql"]
        assert validate_query(sql).ok
