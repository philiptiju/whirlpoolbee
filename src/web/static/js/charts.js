// Claude Local Dashboard - Chart utilities

// Professional color palette
const chartColors = {
    primary: '#6366f1',
    primaryLight: '#818cf8',
    primaryDark: '#4f46e5',
    success: '#10b981',
    successLight: '#34d399',
    danger: '#ef4444',
    warning: '#f59e0b',
    info: '#06b6d4',
    purple: '#8b5cf6',
    pink: '#ec4899',
    gradients: {
        primary: ['#6366f1', '#4f46e5'],
        success: ['#10b981', '#059669'],
        multi: ['#6366f1', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981']
    }
};

// Default chart configuration with modern styling
const defaultChartConfig = {
    responsive: true,
    maintainAspectRatio: true,
    plugins: {
        legend: {
            display: true,
            position: 'top',
            labels: {
                padding: 16,
                font: {
                    family: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
                    size: 13,
                    weight: '500'
                },
                usePointStyle: true,
                pointStyle: 'circle',
                color: '#374151'
            }
        },
        tooltip: {
            backgroundColor: 'rgba(17, 24, 39, 0.95)',
            titleFont: {
                family: "'Inter', sans-serif",
                size: 13,
                weight: '600'
            },
            bodyFont: {
                family: "'Inter', sans-serif",
                size: 12
            },
            padding: 12,
            borderColor: 'rgba(99, 102, 241, 0.5)',
            borderWidth: 1,
            cornerRadius: 8,
            displayColors: true,
            boxPadding: 6
        }
    },
    scales: {
        x: {
            grid: {
                display: false,
                drawBorder: false
            },
            ticks: {
                font: {
                    family: "'Inter', sans-serif",
                    size: 11
                },
                color: '#6b7280'
            }
        },
        y: {
            grid: {
                color: 'rgba(229, 231, 235, 0.8)',
                drawBorder: false
            },
            ticks: {
                font: {
                    family: "'Inter', sans-serif",
                    size: 11
                },
                color: '#6b7280',
                padding: 8
            }
        }
    }
};

// Format large numbers
function formatNumber(num) {
    if (num >= 1000000) {
        return (num / 1000000).toFixed(1) + 'M';
    } else if (num >= 1000) {
        return (num / 1000).toFixed(1) + 'K';
    }
    return num.toString();
}

// Format bytes
function formatBytes(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
}

// Format duration
function formatDuration(seconds) {
    if (!seconds) return 'N/A';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;

    if (hours > 0) {
        return `${hours}h ${minutes}m`;
    } else if (minutes > 0) {
        return `${minutes}m ${secs}s`;
    } else {
        return `${secs}s`;
    }
}

// Error handler for fetch
async function fetchJSON(url) {
    try {
        const response = await fetch(url);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return await response.json();
    } catch (error) {
        console.error('Fetch error:', error);
        return null;
    }
}

// Auto-refresh utility
function setupAutoRefresh(callback, interval = 30000) {
    // Initial load
    callback();

    // Set up interval
    return setInterval(callback, interval);
}

// Export to CSV
function exportTableToCSV(tableId, filename) {
    const table = document.getElementById(tableId);
    if (!table) return;

    let csv = [];
    const rows = table.querySelectorAll('tr');

    for (let row of rows) {
        let cols = row.querySelectorAll('td, th');
        let csvRow = [];
        for (let col of cols) {
            csvRow.push('"' + col.innerText.replace(/"/g, '""') + '"');
        }
        csv.push(csvRow.join(','));
    }

    const csvContent = csv.join('\n');
    const blob = new Blob([csvContent], { type: 'text/csv' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'export.csv';
    a.click();
}

// Export a table's rows to JSON, keyed by header text (mirrors exportTableToCSV).
function exportTableToJSON(tableId, filename) {
    const table = document.getElementById(tableId);
    if (!table) return;

    const headerCells = table.querySelectorAll('thead th');
    const headers = Array.from(headerCells).map(h => h.innerText.trim());
    const bodyRows = table.querySelectorAll('tbody tr');

    const rows = [];
    for (let row of bodyRows) {
        const cells = row.querySelectorAll('td');
        const obj = {};
        cells.forEach((cell, i) => {
            const key = headers[i] || `column_${i + 1}`;
            obj[key] = cell.innerText.trim();
        });
        rows.push(obj);
    }

    const blob = new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'export.json';
    a.click();
    window.URL.revokeObjectURL(url);
}

// Console logging
console.log('Claude Local Dashboard - Charts loaded');
