use std::fmt::{Display, Formatter};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::Serialize;

static DIAGNOSTIC_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub(crate) enum ErrorCategory {
    UnsupportedFeature,
    ResourceLimit,
    Internal,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub(crate) enum ErrorSeverity {
    Blocking,
}

/// Serializable error boundary shared by all Rust commands.
///
/// Model-quality and solver errors originate in the Python domain worker. The
/// host intentionally uses only categories it can diagnose itself.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub(crate) struct PlatformError {
    pub code: &'static str,
    pub category: ErrorCategory,
    pub severity: ErrorSeverity,
    pub user_message: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub technical_message: Option<Box<str>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub object_reference: Option<Box<str>>,
    pub remediation: &'static str,
    pub retryable: bool,
    pub diagnostic_id: Box<str>,
}

impl PlatformError {
    pub(crate) fn worker_unavailable(technical_message: impl Into<String>) -> Self {
        Self::new(
            "PLATFORM_WORKER_UNAVAILABLE",
            ErrorCategory::Internal,
            "The local engineering worker could not be started.",
            technical_message,
            "Restart the application. If the problem continues, create a diagnostics bundle.",
            true,
        )
    }

    pub(crate) fn protocol(technical_message: impl Into<String>) -> Self {
        Self::new(
            "PLATFORM_WORKER_PROTOCOL",
            ErrorCategory::Internal,
            "The local engineering worker returned an unreadable response.",
            technical_message,
            "Restart the application. Completed exact runs remain in the project cache.",
            true,
        )
    }

    pub(crate) fn invalid_request(technical_message: impl Into<String>) -> Self {
        Self::new(
            "PLATFORM_INVALID_REQUEST",
            ErrorCategory::UnsupportedFeature,
            "This application action is not supported by the installed worker.",
            technical_message,
            "Update GI Scenario Studio or return to the previous screen and try again.",
            false,
        )
    }

    pub(crate) fn timeout(method: &str, timeout_ms: u64) -> Self {
        Self::new(
            "PLATFORM_WORKER_TIMEOUT",
            ErrorCategory::ResourceLimit,
            "The local engineering worker did not acknowledge the request in time.",
            format!("method={method}; timeout_ms={timeout_ms}"),
            "Check worker status and retry. Running solver jobs may still complete safely.",
            true,
        )
    }

    pub(crate) fn message_too_large(direction: &str, bytes: usize, limit: usize) -> Self {
        Self::new(
            "PLATFORM_MESSAGE_TOO_LARGE",
            ErrorCategory::ResourceLimit,
            "This request is too large for the local worker protocol.",
            format!("direction={direction}; bytes={bytes}; limit={limit}"),
            "Import large model and GIS files through a project bundle instead of embedding them in an application request.",
            false,
        )
    }

    pub(crate) fn stopped() -> Self {
        Self::new(
            "PLATFORM_WORKER_STOPPED",
            ErrorCategory::Internal,
            "The local engineering worker stopped before the request completed.",
            "worker process ended while a JSON-RPC request was pending",
            "Restart the worker. Completed exact runs remain in the project cache.",
            true,
        )
    }

    fn new(
        code: &'static str,
        category: ErrorCategory,
        user_message: &'static str,
        technical_message: impl Into<String>,
        remediation: &'static str,
        retryable: bool,
    ) -> Self {
        let sequence = DIAGNOSTIC_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        Self {
            code,
            category,
            severity: ErrorSeverity::Blocking,
            user_message,
            technical_message: Some(technical_message.into().into_boxed_str()),
            object_reference: None,
            remediation,
            retryable,
            diagnostic_id: format!("platform-{sequence:08}").into_boxed_str(),
        }
    }
}

impl Display for PlatformError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{} ({})", self.user_message, self.diagnostic_id)
    }
}

impl std::error::Error for PlatformError {}
