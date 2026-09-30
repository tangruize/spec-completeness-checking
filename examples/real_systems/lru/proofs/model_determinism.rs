// Frozen unguarded determinism goals; no inserted assumptions or candidate hints.
include!("../spec/compatible.rs");

verus! {

// Generated equal-fn for determinism check.
// Policy: errs_equivalent=False, opaque_ok=False
spec fn __specdet_5e44ebfb5b3ef49c80327f36_equal(r1: Option<u64>, r2: Option<u64>, post1_cache: Cache, post2_cache: Cache) -> bool {
    (((r1 is Some) == (r2 is Some)) && ((r1 is Some) ==> (r1->Some_0 == r2->Some_0)))
    && (((post1_cache).view() =~= (post2_cache).view()))
}

proof fn __specdet_5e44ebfb5b3ef49c80327f36(pre_cache: Cache, key: u64, value: u64, post1_cache: Cache, r1: Option<u64>, post2_cache: Cache, r2: Option<u64>)
    requires (pre_cache.wf()),
    ensures
        ({
            &&& (post1_cache.wf())
            &&& (post1_cache.capacity == pre_cache.capacity)
            &&& (r1 == lookup(&pre_cache, key))
            &&& (post1_cache.entries@ == (
            if pre_cache.entries@.dom().contains(key) {
                pre_cache.entries@.insert(key, value)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.entries@.remove(pre_cache.order@.last()).insert(key, value)
            } else {
                pre_cache.entries@.insert(key, value)
            }
        ))
            &&& (post1_cache.order@ == Seq::<u64>::empty().push(key) + (
            if pre_cache.entries@.dom().contains(key) {
                without(pre_cache.order@, key)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.order@.drop_last()
            } else {
                pre_cache.order@
            }
        ))
            &&& (post2_cache.wf())
            &&& (post2_cache.capacity == pre_cache.capacity)
            &&& (r2 == lookup(&pre_cache, key))
            &&& (post2_cache.entries@ == (
            if pre_cache.entries@.dom().contains(key) {
                pre_cache.entries@.insert(key, value)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.entries@.remove(pre_cache.order@.last()).insert(key, value)
            } else {
                pre_cache.entries@.insert(key, value)
            }
        ))
            &&& (post2_cache.order@ == Seq::<u64>::empty().push(key) + (
            if pre_cache.entries@.dom().contains(key) {
                without(pre_cache.order@, key)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.order@.drop_last()
            } else {
                pre_cache.order@
            }
        ))
        }) ==> __specdet_5e44ebfb5b3ef49c80327f36_equal(r1, r2, post1_cache, post2_cache),
{
}

// Generated equal-fn for determinism check.
// Policy: errs_equivalent=False, opaque_ok=False
spec fn __specdet_ddb9e1b0e2aeb9cf092abee3_equal(r1: Option<u64>, r2: Option<u64>, post1_cache: Cache, post2_cache: Cache) -> bool {
    (((r1 is Some) == (r2 is Some)) && ((r1 is Some) ==> (r1->Some_0 == r2->Some_0)))
    && (((post1_cache).view() =~= (post2_cache).view()))
}

proof fn __specdet_ddb9e1b0e2aeb9cf092abee3(pre_cache: Cache, key: u64, post1_cache: Cache, r1: Option<u64>, post2_cache: Cache, r2: Option<u64>)
    requires (pre_cache.wf()),
    ensures
        ({
            &&& (post1_cache.wf())
            &&& (post1_cache.capacity == pre_cache.capacity)
            &&& (r1 == lookup(&pre_cache, key))
            &&& (post1_cache.entries@ == pre_cache.entries@)
            &&& (post1_cache.order@ == (
            if pre_cache.entries@.dom().contains(key) {
                Seq::<u64>::empty().push(key) + without(pre_cache.order@, key)
            } else {
                pre_cache.order@
            }
        ))
            &&& (post2_cache.wf())
            &&& (post2_cache.capacity == pre_cache.capacity)
            &&& (r2 == lookup(&pre_cache, key))
            &&& (post2_cache.entries@ == pre_cache.entries@)
            &&& (post2_cache.order@ == (
            if pre_cache.entries@.dom().contains(key) {
                Seq::<u64>::empty().push(key) + without(pre_cache.order@, key)
            } else {
                pre_cache.order@
            }
        ))
        }) ==> __specdet_ddb9e1b0e2aeb9cf092abee3_equal(r1, r2, post1_cache, post2_cache),
{
}

// Generated equal-fn for determinism check.
// Policy: errs_equivalent=False, opaque_ok=False
spec fn __specdet_ecc005e2b0714df58beed50d_equal(r1: Option<u64>, r2: Option<u64>, post1_cache: Cache, post2_cache: Cache) -> bool {
    (((r1 is Some) == (r2 is Some)) && ((r1 is Some) ==> (r1->Some_0 == r2->Some_0)))
    && (((post1_cache).view() =~= (post2_cache).view()))
}

proof fn __specdet_ecc005e2b0714df58beed50d(pre_cache: Cache, key: u64, post1_cache: Cache, r1: Option<u64>, post2_cache: Cache, r2: Option<u64>)
    requires (pre_cache.wf()),
    ensures
        ({
            &&& (post1_cache.wf())
            &&& (post1_cache.capacity == pre_cache.capacity)
            &&& (r1 == lookup(&pre_cache, key))
            &&& (post1_cache.entries@ == pre_cache.entries@.remove(key))
            &&& (post1_cache.order@ == without(pre_cache.order@, key))
            &&& (post2_cache.wf())
            &&& (post2_cache.capacity == pre_cache.capacity)
            &&& (r2 == lookup(&pre_cache, key))
            &&& (post2_cache.entries@ == pre_cache.entries@.remove(key))
            &&& (post2_cache.order@ == without(pre_cache.order@, key))
        }) ==> __specdet_ecc005e2b0714df58beed50d_equal(r1, r2, post1_cache, post2_cache),
{
}

}
