// Generated from frozen source-derived postconditions; no target is called.
include!("../spec/compatible.rs");

verus! {

proof fn put_empty() {
    broadcast use {vstd::map::group_map_axioms, vstd::set::group_set_axioms,
        vstd::seq_lib::group_seq_properties, vstd::seq_lib::group_seq_lib_default};
    reveal_with_fuel(Seq::<_>::filter, 5);
    let pre_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty()), order: Ghost(Seq::<u64>::empty()) };
    let key: u64 = 0u64;
    let value: u64 = 0u64;
    let post1_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(0u64, 0u64)), order: Ghost(Seq::<u64>::empty().push(0u64)) };
    let r1: Option<u64> = Option::<u64>::None;
    let post2_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(0u64, 0u64)), order: Ghost(Seq::<u64>::empty().push(0u64)) };
    let r2: Option<u64> = Option::<u64>::None;
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
            &&& (r1 == lookup(&pre_cache, key))
            &&& (post1_cache.entries@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                pre_cache.entries@.insert(key, value)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.entries@.remove(pre_cache.order@.last()).insert(key, value)
            } else {
                pre_cache.entries@.insert(key, value)
            }
        ))
            &&& (post1_cache.order@ =~= Seq::<u64>::empty().push(key) + (
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
            &&& (post2_cache.entries@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                pre_cache.entries@.insert(key, value)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.entries@.remove(pre_cache.order@.last()).insert(key, value)
            } else {
                pre_cache.entries@.insert(key, value)
            }
        ))
            &&& (post2_cache.order@ =~= Seq::<u64>::empty().push(key) + (
            if pre_cache.entries@.dom().contains(key) {
                without(pre_cache.order@, key)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.order@.drop_last()
            } else {
                pre_cache.order@
            }
        ))
        }));
    assert(({
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
        }));
}

proof fn put_nonfull() {
    broadcast use {vstd::map::group_map_axioms, vstd::set::group_set_axioms,
        vstd::seq_lib::group_seq_properties, vstd::seq_lib::group_seq_lib_default};
    reveal_with_fuel(Seq::<_>::filter, 5);
    let pre_cache: Cache = Cache { capacity: 3usize, entries: Ghost(Map::<u64, u64>::empty().insert(1u64, 1u64).insert(0u64, 0u64)), order: Ghost(Seq::<u64>::empty().push(1u64).push(0u64)) };
    let key: u64 = 2u64;
    let value: u64 = 2u64;
    let post1_cache: Cache = Cache { capacity: 3usize, entries: Ghost(Map::<u64, u64>::empty().insert(2u64, 2u64).insert(1u64, 1u64).insert(0u64, 0u64)), order: Ghost(Seq::<u64>::empty().push(2u64).push(1u64).push(0u64)) };
    let r1: Option<u64> = Option::<u64>::None;
    let post2_cache: Cache = Cache { capacity: 3usize, entries: Ghost(Map::<u64, u64>::empty().insert(2u64, 2u64).insert(1u64, 1u64).insert(0u64, 0u64)), order: Ghost(Seq::<u64>::empty().push(2u64).push(1u64).push(0u64)) };
    let r2: Option<u64> = Option::<u64>::None;
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
            &&& (r1 == lookup(&pre_cache, key))
            &&& (post1_cache.entries@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                pre_cache.entries@.insert(key, value)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.entries@.remove(pre_cache.order@.last()).insert(key, value)
            } else {
                pre_cache.entries@.insert(key, value)
            }
        ))
            &&& (post1_cache.order@ =~= Seq::<u64>::empty().push(key) + (
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
            &&& (post2_cache.entries@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                pre_cache.entries@.insert(key, value)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.entries@.remove(pre_cache.order@.last()).insert(key, value)
            } else {
                pre_cache.entries@.insert(key, value)
            }
        ))
            &&& (post2_cache.order@ =~= Seq::<u64>::empty().push(key) + (
            if pre_cache.entries@.dom().contains(key) {
                without(pre_cache.order@, key)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.order@.drop_last()
            } else {
                pre_cache.order@
            }
        ))
        }));
    assert(({
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
        }));
}

proof fn put_update_full() {
    broadcast use {vstd::map::group_map_axioms, vstd::set::group_set_axioms,
        vstd::seq_lib::group_seq_properties, vstd::seq_lib::group_seq_lib_default};
    reveal_with_fuel(Seq::<_>::filter, 5);
    let pre_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(1u64, 1u64).insert(0u64, 0u64)), order: Ghost(Seq::<u64>::empty().push(1u64).push(0u64)) };
    let key: u64 = 0u64;
    let value: u64 = 2u64;
    let post1_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(0u64, 2u64).insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(0u64).push(1u64)) };
    let r1: Option<u64> = Option::<u64>::Some(0u64);
    let post2_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(0u64, 2u64).insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(0u64).push(1u64)) };
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
            &&& (r1 == lookup(&pre_cache, key))
            &&& (post1_cache.entries@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                pre_cache.entries@.insert(key, value)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.entries@.remove(pre_cache.order@.last()).insert(key, value)
            } else {
                pre_cache.entries@.insert(key, value)
            }
        ))
            &&& (post1_cache.order@ =~= Seq::<u64>::empty().push(key) + (
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
            &&& (post2_cache.entries@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                pre_cache.entries@.insert(key, value)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.entries@.remove(pre_cache.order@.last()).insert(key, value)
            } else {
                pre_cache.entries@.insert(key, value)
            }
        ))
            &&& (post2_cache.order@ =~= Seq::<u64>::empty().push(key) + (
            if pre_cache.entries@.dom().contains(key) {
                without(pre_cache.order@, key)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.order@.drop_last()
            } else {
                pre_cache.order@
            }
        ))
        }));
    assert(({
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
        }));
}

proof fn put_evict() {
    broadcast use {vstd::map::group_map_axioms, vstd::set::group_set_axioms,
        vstd::seq_lib::group_seq_properties, vstd::seq_lib::group_seq_lib_default};
    reveal_with_fuel(Seq::<_>::filter, 5);
    let pre_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(1u64, 1u64).insert(0u64, 0u64)), order: Ghost(Seq::<u64>::empty().push(1u64).push(0u64)) };
    let key: u64 = 2u64;
    let value: u64 = 2u64;
    let post1_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(2u64, 2u64).insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(2u64).push(1u64)) };
    let r1: Option<u64> = Option::<u64>::None;
    let post2_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(2u64, 2u64).insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(2u64).push(1u64)) };
    let r2: Option<u64> = Option::<u64>::None;
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
            &&& (r1 == lookup(&pre_cache, key))
            &&& (post1_cache.entries@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                pre_cache.entries@.insert(key, value)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.entries@.remove(pre_cache.order@.last()).insert(key, value)
            } else {
                pre_cache.entries@.insert(key, value)
            }
        ))
            &&& (post1_cache.order@ =~= Seq::<u64>::empty().push(key) + (
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
            &&& (post2_cache.entries@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                pre_cache.entries@.insert(key, value)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.entries@.remove(pre_cache.order@.last()).insert(key, value)
            } else {
                pre_cache.entries@.insert(key, value)
            }
        ))
            &&& (post2_cache.order@ =~= Seq::<u64>::empty().push(key) + (
            if pre_cache.entries@.dom().contains(key) {
                without(pre_cache.order@, key)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.order@.drop_last()
            } else {
                pre_cache.order@
            }
        ))
        }));
    assert(({
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
        }));
}

proof fn put_capacity_one() {
    broadcast use {vstd::map::group_map_axioms, vstd::set::group_set_axioms,
        vstd::seq_lib::group_seq_properties, vstd::seq_lib::group_seq_lib_default};
    reveal_with_fuel(Seq::<_>::filter, 5);
    let pre_cache: Cache = Cache { capacity: 1usize, entries: Ghost(Map::<u64, u64>::empty().insert(0u64, 0u64)), order: Ghost(Seq::<u64>::empty().push(0u64)) };
    let key: u64 = 1u64;
    let value: u64 = 1u64;
    let post1_cache: Cache = Cache { capacity: 1usize, entries: Ghost(Map::<u64, u64>::empty().insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(1u64)) };
    let r1: Option<u64> = Option::<u64>::None;
    let post2_cache: Cache = Cache { capacity: 1usize, entries: Ghost(Map::<u64, u64>::empty().insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(1u64)) };
    let r2: Option<u64> = Option::<u64>::None;
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
            &&& (r1 == lookup(&pre_cache, key))
            &&& (post1_cache.entries@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                pre_cache.entries@.insert(key, value)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.entries@.remove(pre_cache.order@.last()).insert(key, value)
            } else {
                pre_cache.entries@.insert(key, value)
            }
        ))
            &&& (post1_cache.order@ =~= Seq::<u64>::empty().push(key) + (
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
            &&& (post2_cache.entries@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                pre_cache.entries@.insert(key, value)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.entries@.remove(pre_cache.order@.last()).insert(key, value)
            } else {
                pre_cache.entries@.insert(key, value)
            }
        ))
            &&& (post2_cache.order@ =~= Seq::<u64>::empty().push(key) + (
            if pre_cache.entries@.dom().contains(key) {
                without(pre_cache.order@, key)
            } else if pre_cache.order@.len() == pre_cache.capacity {
                pre_cache.order@.drop_last()
            } else {
                pre_cache.order@
            }
        ))
        }));
    assert(({
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
        }));
}

proof fn get_hit() {
    broadcast use {vstd::map::group_map_axioms, vstd::set::group_set_axioms,
        vstd::seq_lib::group_seq_properties, vstd::seq_lib::group_seq_lib_default};
    reveal_with_fuel(Seq::<_>::filter, 5);
    let pre_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(1u64, 1u64).insert(0u64, 0u64)), order: Ghost(Seq::<u64>::empty().push(1u64).push(0u64)) };
    let key: u64 = 0u64;
    let post1_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(0u64, 0u64).insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(0u64).push(1u64)) };
    let r1: Option<u64> = Option::<u64>::Some(0u64);
    let post2_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(0u64, 0u64).insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(0u64).push(1u64)) };
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
            &&& (r1 == lookup(&pre_cache, key))
            &&& (post1_cache.entries@ =~= pre_cache.entries@)
            &&& (post1_cache.order@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                Seq::<u64>::empty().push(key) + without(pre_cache.order@, key)
            } else {
                pre_cache.order@
            }
        ))
            &&& (post2_cache.wf())
            &&& (post2_cache.capacity == pre_cache.capacity)
            &&& (r2 == lookup(&pre_cache, key))
            &&& (post2_cache.entries@ =~= pre_cache.entries@)
            &&& (post2_cache.order@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                Seq::<u64>::empty().push(key) + without(pre_cache.order@, key)
            } else {
                pre_cache.order@
            }
        ))
        }));
    assert(({
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
        }));
}

proof fn get_miss() {
    broadcast use {vstd::map::group_map_axioms, vstd::set::group_set_axioms,
        vstd::seq_lib::group_seq_properties, vstd::seq_lib::group_seq_lib_default};
    reveal_with_fuel(Seq::<_>::filter, 5);
    let pre_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(1u64)) };
    let key: u64 = 0u64;
    let post1_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(1u64)) };
    let r1: Option<u64> = Option::<u64>::None;
    let post2_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(1u64)) };
    let r2: Option<u64> = Option::<u64>::None;
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
            &&& (r1 == lookup(&pre_cache, key))
            &&& (post1_cache.entries@ =~= pre_cache.entries@)
            &&& (post1_cache.order@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                Seq::<u64>::empty().push(key) + without(pre_cache.order@, key)
            } else {
                pre_cache.order@
            }
        ))
            &&& (post2_cache.wf())
            &&& (post2_cache.capacity == pre_cache.capacity)
            &&& (r2 == lookup(&pre_cache, key))
            &&& (post2_cache.entries@ =~= pre_cache.entries@)
            &&& (post2_cache.order@ =~= (
            if pre_cache.entries@.dom().contains(key) {
                Seq::<u64>::empty().push(key) + without(pre_cache.order@, key)
            } else {
                pre_cache.order@
            }
        ))
        }));
    assert(({
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
        }));
}

proof fn pop_hit() {
    broadcast use {vstd::map::group_map_axioms, vstd::set::group_set_axioms,
        vstd::seq_lib::group_seq_properties, vstd::seq_lib::group_seq_lib_default};
    reveal_with_fuel(Seq::<_>::filter, 5);
    let pre_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(1u64, 1u64).insert(0u64, 0u64)), order: Ghost(Seq::<u64>::empty().push(1u64).push(0u64)) };
    let key: u64 = 0u64;
    let post1_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(1u64)) };
    let r1: Option<u64> = Option::<u64>::Some(0u64);
    let post2_cache: Cache = Cache { capacity: 2usize, entries: Ghost(Map::<u64, u64>::empty().insert(1u64, 1u64)), order: Ghost(Seq::<u64>::empty().push(1u64)) };
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
            &&& (r1 == lookup(&pre_cache, key))
            &&& (post1_cache.entries@ =~= pre_cache.entries@.remove(key))
            &&& (post1_cache.order@ =~= without(pre_cache.order@, key))
            &&& (post2_cache.wf())
            &&& (post2_cache.capacity == pre_cache.capacity)
            &&& (r2 == lookup(&pre_cache, key))
            &&& (post2_cache.entries@ =~= pre_cache.entries@.remove(key))
            &&& (post2_cache.order@ =~= without(pre_cache.order@, key))
        }));
    assert(({
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
        }));
}

proof fn pop_miss() {
    broadcast use {vstd::map::group_map_axioms, vstd::set::group_set_axioms,
        vstd::seq_lib::group_seq_properties, vstd::seq_lib::group_seq_lib_default};
    reveal_with_fuel(Seq::<_>::filter, 5);
    let pre_cache: Cache = Cache { capacity: 1usize, entries: Ghost(Map::<u64, u64>::empty()), order: Ghost(Seq::<u64>::empty()) };
    let key: u64 = 0u64;
    let post1_cache: Cache = Cache { capacity: 1usize, entries: Ghost(Map::<u64, u64>::empty()), order: Ghost(Seq::<u64>::empty()) };
    let r1: Option<u64> = Option::<u64>::None;
    let post2_cache: Cache = Cache { capacity: 1usize, entries: Ghost(Map::<u64, u64>::empty()), order: Ghost(Seq::<u64>::empty()) };
    let r2: Option<u64> = Option::<u64>::None;
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
            &&& (r1 == lookup(&pre_cache, key))
            &&& (post1_cache.entries@ =~= pre_cache.entries@.remove(key))
            &&& (post1_cache.order@ =~= without(pre_cache.order@, key))
            &&& (post2_cache.wf())
            &&& (post2_cache.capacity == pre_cache.capacity)
            &&& (r2 == lookup(&pre_cache, key))
            &&& (post2_cache.entries@ =~= pre_cache.entries@.remove(key))
            &&& (post2_cache.order@ =~= without(pre_cache.order@, key))
        }));
    assert(({
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
        }));
}

}
