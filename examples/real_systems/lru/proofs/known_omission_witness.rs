// Deliberately supplied witness for the labeled mutation, NOT automatically discovered.
include!("../controls/pop_return_omitted.rs");

verus! {

// Generated equal-fn for determinism check.
// Policy: errs_equivalent=False, opaque_ok=False
spec fn __specdet_d88d7c1743e1d8aaeb56b287_equal(r1: Option<u64>, r2: Option<u64>, post1_cache: Cache, post2_cache: Cache) -> bool {
    (((r1 is Some) == (r2 is Some)) && ((r1 is Some) ==> (r1->Some_0 == r2->Some_0)))
    && (((post1_cache).view() =~= (post2_cache).view()))
}

proof fn known_pop_return_omission() {
    broadcast use {vstd::map::group_map_axioms, vstd::set::group_set_axioms,
        vstd::seq_lib::group_seq_properties, vstd::seq_lib::group_seq_lib_default};
    reveal_with_fuel(Seq::<_>::filter, 5);
    let pre_cache: Cache = Cache { capacity: 1usize, entries: Ghost(Map::<u64, u64>::empty()), order: Ghost(Seq::<u64>::empty()) };
    let key: u64 = 0u64;
    let post1_cache: Cache = Cache { capacity: 1usize, entries: Ghost(Map::<u64, u64>::empty()), order: Ghost(Seq::<u64>::empty()) };
    let r1: Option<u64> = Option::<u64>::None;
    let post2_cache: Cache = Cache { capacity: 1usize, entries: Ghost(Map::<u64, u64>::empty()), order: Ghost(Seq::<u64>::empty()) };
    let r2: Option<u64> = Option::<u64>::Some(0u64);
    assert(pre_cache.entries@.dom() =~= pre_cache.order@.to_set());
    assert(pre_cache.wf());
    assert(post1_cache.entries@.dom() =~= post1_cache.order@.to_set());
    assert(post1_cache.wf());
    assert(post2_cache.entries@.dom() =~= post2_cache.order@.to_set());
    assert(post2_cache.wf());
    assert((pre_cache.wf()));
    assert(({
            &&& (post1_cache.wf())
            &&& (post1_cache.capacity == pre_cache.capacity)
            &&& (post1_cache.entries@ =~= pre_cache.entries@.remove(key))
            &&& (post1_cache.order@ =~= without(pre_cache.order@, key))
            &&& (post2_cache.wf())
            &&& (post2_cache.capacity == pre_cache.capacity)
            &&& (post2_cache.entries@ =~= pre_cache.entries@.remove(key))
            &&& (post2_cache.order@ =~= without(pre_cache.order@, key))
        }));
    assert(({
            &&& (post1_cache.wf())
            &&& (post1_cache.capacity == pre_cache.capacity)
            &&& (post1_cache.entries@ == pre_cache.entries@.remove(key))
            &&& (post1_cache.order@ == without(pre_cache.order@, key))
            &&& (post2_cache.wf())
            &&& (post2_cache.capacity == pre_cache.capacity)
            &&& (post2_cache.entries@ == pre_cache.entries@.remove(key))
            &&& (post2_cache.order@ == without(pre_cache.order@, key))
        }));
    assert(!(__specdet_d88d7c1743e1d8aaeb56b287_equal(r1, r2, post1_cache, post2_cache)));
}

}
