use vstd::prelude::*;

fn main() {}

verus! {

pub struct PersistentMemoryByte {
    pub state_at_last_flush: u8,
    pub outstanding_write: Option<u8>,
}

pub struct PersistentMemoryRegionView {
    pub state: Seq<PersistentMemoryByte>,
}

impl PersistentMemoryRegionView {
    pub open spec fn len(self) -> nat {
        self.state.len()
    }
}

pub struct PersistentMemoryRegionsView {
    pub regions: Seq<PersistentMemoryRegionView>,
}

impl PersistentMemoryRegionsView {
    pub open spec fn len(self) -> nat {
        self.regions.len()
    }

    pub open spec fn spec_index(self, i: int) -> PersistentMemoryRegionView {
        self.regions[i]
    }

    spec fn view(&self) -> PersistentMemoryRegionsView;

    spec fn inv(&self) -> bool;

    #[verifier::external_body]
    fn get_num_regions(&self) -> (result: usize)
        requires
            self.inv(),
        ensures
            result == self@.len(),
    {
        unimplemented!()
    }

    #[verifier::external_body]
    fn get_region_size(&self, index: usize) -> (result: u64)
        requires
            self.inv(),
            index < self@.len(),
        ensures
            result == self@[index as int].len(),
    {
        unimplemented!()
    }
}

pub trait PersistentMemoryRegions: Sized {
    spec fn view(&self) -> PersistentMemoryRegionsView;

    spec fn inv(&self) -> bool;

    fn get_num_regions(&self) -> (result: usize)
        requires
            self.inv(),
        ensures
            result == self@.len(),
    ;

    fn get_region_size(&self, index: usize) -> (result: u64)
        requires
            self.inv(),
            index < self@.len(),
        ensures
            result == self@[index as int].len(),
    ;
}

/*pmem\pmemutil_v*/

#[verifier::external_body]
pub fn get_region_sizes<PMRegions: PersistentMemoryRegions>(pm_regions: &PMRegions) -> (result: Vec<
    u64,
>)
    requires
        pm_regions.inv(),
    ensures
        result@.len() == pm_regions@.len(),
        forall|i: int| 0 <= i < pm_regions@.len() ==> result@[i] == #[trigger] pm_regions@[i].len(),
{
    unimplemented!()
}

}
