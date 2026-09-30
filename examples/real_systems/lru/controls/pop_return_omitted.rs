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

}

fn main() {}
