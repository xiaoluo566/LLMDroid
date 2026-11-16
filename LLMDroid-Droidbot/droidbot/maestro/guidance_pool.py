"""The ranked FunctionList shown to both LLMDroid Guidance and MAESTRO."""


def guidance_overview(ranked_clusters):
    overview = {}
    for cluster in ranked_clusters:
        if cluster.has_untested_function():
            cluster.write_overview_top5_tojson(overview)
    if not overview:
        for cluster in ranked_clusters:
            cluster.write_overview_top5_tojson(overview, ignore_importance=True)
    return overview
