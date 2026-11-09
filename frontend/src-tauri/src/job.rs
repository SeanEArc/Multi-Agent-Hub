//! A Windows job object that kills its processes when closed.
//!
//! The backend is assigned to a new job on every start. Its children (agent CLIs, git) join the
//! same job, so dropping the `Job`, or the shell exiting in any way, kills the whole tree.

use std::process::Child;

#[cfg(windows)]
pub struct Job(windows_sys::Win32::Foundation::HANDLE);

// The handle is only closed, from whichever thread drops the Job.
#[cfg(windows)]
unsafe impl Send for Job {}

#[cfg(windows)]
impl Job {
    pub fn for_process(child: &Child) -> Result<Job, String> {
        use std::mem::{size_of, zeroed};
        use std::os::windows::io::AsRawHandle;
        use std::ptr::null;
        use windows_sys::Win32::Foundation::GetLastError;
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };

        unsafe {
            let handle = CreateJobObjectW(null(), null());
            if handle.is_null() {
                return Err(format!("CreateJobObjectW failed ({})", GetLastError()));
            }
            let job = Job(handle);
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            ) == 0
            {
                return Err(format!("SetInformationJobObject failed ({})", GetLastError()));
            }
            if AssignProcessToJobObject(handle, child.as_raw_handle() as _) == 0 {
                return Err(format!("AssignProcessToJobObject failed ({})", GetLastError()));
            }
            Ok(job)
        }
    }
}

#[cfg(windows)]
impl Drop for Job {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}

#[cfg(not(windows))]
pub struct Job;

#[cfg(not(windows))]
impl Job {
    pub fn for_process(_child: &Child) -> Result<Job, String> {
        Ok(Job)
    }
}
