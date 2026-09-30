use vstd::prelude::*;
use vstd::seq_lib::*;

verus! {

pub struct Cache {
    pub capacity: usize,
    pub entries: Ghost<Map<u64, u64>>,
    pub order: Ghost<Seq<u64>>,
}

impl View for Cache {
    type V = (usize, Map<u64, u64>, Seq<u64>);

    open spec fn view(&self) -> Self::V {
        (self.capacity, self.entries@, self.order@)
    }
}

impl Cache {
    pub open spec fn wf(&self) -> bool {
        &&& self.capacity > 0
        &&& self.entries@.dom().finite()
        &&& self.entries@.dom() == self.order@.to_set()
        &&& self.order@.no_duplicates()
        &&& self.order@.len() <= self.capacity
    }
}

pub open spec fn without(order: Seq<u64>, key: u64) -> Seq<u64> {
    order.filter(|item: u64| item != key)
}

pub open spec fn lookup(cache: &Cache, key: u64) -> Option<u64> {
    if cache.entries@.dom().contains(key) {
        Some(cache.entries@[key])
    } else {
        None
    }
}

// Deliberate sensitivity control: the pop return-value clause alone is omitted.
#[verifier::external_body]
pub fn pop(cache: &mut Cache, key: u64) -> (result: Option<u64>)
    requires
        old(cache).wf(),
    ensures
        final(cache).wf(),
        final(cache).capacity == old(cache).capacity,
        final(cache).entries@ == old(cache).entries@.remove(key),
        final(cache).order@ == without(old(cache).order@, key),
{
    unimplemented!()
}

// Generated equal-fn for determinism check.
// Policy: errs_equivalent=False, opaque_ok=False
spec fn __specdet_d88d7c1743e1d8aaeb56b287_equal(r1: Option<u64>, r2: Option<u64>, post1_cache: Cache, post2_cache: Cache) -> bool {
    (((r1 is Some) == (r2 is Some)) && ((r1 is Some) ==> (r1->Some_0 == r2->Some_0)))
    && (((post1_cache).view() =~= (post2_cache).view()))
}

fn __specdet_witness_4c8767739cc48d238b0f() {
    let ghost __specdet_value_0 = Map::empty();
    let ghost __specdet_value_1 = Seq::empty();
    let ghost pre_cache: Cache = Cache { capacity: 1, entries: Ghost(__specdet_value_0), order: Ghost(__specdet_value_1) };
    let ghost key: u64 = 0;
    let ghost __specdet_value_2 = Map::empty();
    let ghost __specdet_value_3 = Seq::empty();
    let ghost post1_cache: Cache = Cache { capacity: 1, entries: Ghost(__specdet_value_2), order: Ghost(__specdet_value_3) };
    let ghost r1: Option<u64> = Some(0);
    let ghost __specdet_value_4 = Map::empty();
    let ghost __specdet_value_5 = Seq::empty();
    let ghost post2_cache: Cache = Cache { capacity: 1, entries: Ghost(__specdet_value_4), order: Ghost(__specdet_value_5) };
    let ghost r2: Option<u64> = Some(1);
    proof {
        broadcast use vstd::map::group_map_axioms;
        broadcast use vstd::set::group_set_axioms;
        broadcast use vstd::seq_lib::group_seq_properties;
        broadcast use vstd::seq_lib::group_seq_lib_default;
        reveal_with_fuel(vstd::seq::Seq::<_>::filter, 5);
        assert((pre_cache).capacity == 1);
        assert((((pre_cache).entries)@).dom().is_empty());
        assert((((pre_cache).entries)@).dom() =~= Set::empty());
        assert((((pre_cache).order)@).len() == 0);
        assert((((pre_cache).order)@).to_set() =~= Set::empty());
        assert(key == 0);
        assert((post1_cache).capacity == 1);
        assert((((post1_cache).entries)@).dom().is_empty());
        assert((((post1_cache).entries)@).dom() =~= Set::empty());
        assert((((post1_cache).order)@).len() == 0);
        assert((((post1_cache).order)@).to_set() =~= Set::empty());
        assert((post2_cache).capacity == 1);
        assert((((post2_cache).entries)@).dom().is_empty());
        assert((((post2_cache).entries)@).dom() =~= Set::empty());
        assert((((post2_cache).order)@).len() == 0);
        assert((((post2_cache).order)@).to_set() =~= Set::empty());
        assert((pre_cache.wf()));
        assert((post1_cache.entries@) =~= (pre_cache.entries@.remove(key)));
        assert((post1_cache.order@) =~= (without(pre_cache.order@, key)));
        assert((post2_cache.entries@) =~= (pre_cache.entries@.remove(key)));
        assert((post2_cache.order@) =~= (without(pre_cache.order@, key)));
        assert((post1_cache.wf()));
        assert((post1_cache.capacity == pre_cache.capacity));
        assert((post1_cache.entries@ == pre_cache.entries@.remove(key)));
        assert((post1_cache.order@ == without(pre_cache.order@, key)));
        assert((post2_cache.wf()));
        assert((post2_cache.capacity == pre_cache.capacity));
        assert((post2_cache.entries@ == pre_cache.entries@.remove(key)));
        assert((post2_cache.order@ == without(pre_cache.order@, key)));
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
        reveal(__specdet_d88d7c1743e1d8aaeb56b287_equal);
        assert(!(__specdet_d88d7c1743e1d8aaeb56b287_equal(r1, r2, post1_cache, post2_cache)));
    }
}



}

fn main() {}
