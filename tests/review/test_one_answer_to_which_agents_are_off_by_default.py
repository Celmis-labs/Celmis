"""Two branches of 2.3.0 each wrote down which finders are off by default:
the settings model (`review_defaults.AGENT_PARTICIPATION_DEFAULTS`, what the
API and the settings pages show) and the orchestrator
(`ReviewOrchestrator.OFF_BY_DEFAULT`, what a review actually runs). They agree
today; this keeps them agreeing, so the page never shows an agent as running
that the review leaves dormant, or the other way round."""

from src.review.orchestrator import ReviewOrchestrator
from src.review.review_defaults import AGENT_PARTICIPATION_DEFAULTS


def test_the_page_and_the_review_agree_on_the_dormant_agents():
    off_in_settings = {a for a, on in AGENT_PARTICIPATION_DEFAULTS.items() if not on}
    assert set(ReviewOrchestrator.OFF_BY_DEFAULT) == off_in_settings
