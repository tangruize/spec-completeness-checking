use vstd::prelude::*;

fn main() {}

verus! {

pub type VAddr = usize;

pub const KERNEL_MEM_END_L4INDEX: usize = 1;

pub const MEM_4k_MASK: u64 = 0x0000_ffff_ffff_f000;

#[derive(Clone, Copy)]
pub struct VaRange4K {
    pub start: VAddr,
    pub len: usize,
    pub view: Ghost<Seq<VAddr>>,
}

impl VaRange4K {

	#[verifier::external_body]
    pub closed spec fn view(&self) -> Seq<VAddr> {
		unimplemented!()
	}


    pub open spec fn wf(&self) -> bool {
        &&& self.start + self.len * 4096 < usize::MAX
        &&& spec_va_4k_valid(self.start)
        &&& self@.len() == self.len
        &&& self@.no_duplicates()
        &&& forall|i: int| #![trigger self@[i]] 0 <= i < self.len ==> spec_va_4k_valid(self@[i])
        &&& self.view_match_spec()
    }

	#[verifier::external_body]
    pub closed spec fn view_match_spec(&self) -> bool {
		unimplemented!()
	}


	#[verifier::external_body]
    pub fn index(&self, i: usize) -> (ret: VAddr)
        requires
            self.wf(),
            0 <= i < self.len,
        ensures
            ret == self@[i as int],
	{
		unimplemented!()
	}

}

pub open spec fn spec_va_4k_valid(va: usize) -> bool {
    (va & (!MEM_4k_MASK) as usize == 0) && (va as u64 >> 39u64 & 0x1ffu64)
        >= KERNEL_MEM_END_L4INDEX as u64
}

}
