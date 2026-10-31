"""Response-cache keys. Pure: no I/O, no AWS."""

import hashlib
import json


def model_cache_key(*, owner, model_id, system, messages, inference_config, additional_fields):
    # Every input that can change a reply is part of the key, including the owner so one
    # user's cached replies are never served to another.
    request = {
        "owner": owner,
        "modelId": model_id,
        "system": system,
        "messages": messages,
        "inferenceConfig": inference_config,
        "additionalModelRequestFields": additional_fields,
    }
    # Sorted keys make dict order irrelevant. The hash gives a fixed 67-character key
    # with no spaces, inside Memcached's 250-byte key limit.
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "pg:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
