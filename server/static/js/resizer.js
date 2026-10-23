export class PanelResizer {
    constructor() {
        this.resizers = document.querySelectorAll('.resizer');
        this.leftPanel = document.querySelector('.left-panel');
        this.middlePanel = document.querySelector('.middle-panel');
        this.rightPanel = document.querySelector('.right-panel');

        this.isResizing = false;
        this.currentResizer = null;

        this.init();
    }

    init() {
        this.resizers.forEach(resizer => {
            resizer.addEventListener('mousedown', (e) => this.startResize(e, resizer));
        });

        document.addEventListener('mousemove', (e) => this.resize(e));
        document.addEventListener('mouseup', () => this.stopResize());
    }

    startResize(e, resizer) {
        this.isResizing = true;
        this.currentResizer = resizer;
        resizer.classList.add('resizing');

        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';

        e.preventDefault();
    }

    resize(e) {
        if (!this.isResizing || !this.currentResizer) return;

        const direction = this.currentResizer.dataset.direction;
        const containerRect = this.leftPanel.parentElement.getBoundingClientRect();

        if (direction === 'left') {
            // Resizing between left and middle panels
            const newLeftWidth = e.clientX - containerRect.left;
            const minLeftWidth = 200;
            const minMiddleWidth = 400;
            const rightWidth = this.rightPanel.getBoundingClientRect().width;
            const maxLeftWidth = containerRect.width - minMiddleWidth - rightWidth - 8;

            if (newLeftWidth >= minLeftWidth && newLeftWidth <= maxLeftWidth) {
                this.leftPanel.style.flex = 'none';
                this.leftPanel.style.width = `${newLeftWidth}px`;
            }
        } else if (direction === 'right') {
            // Resizing between middle and right panels
            const newRightWidth = containerRect.right - e.clientX;
            const minRightWidth = 300;
            const minMiddleWidth = 400;
            const leftWidth = this.leftPanel.getBoundingClientRect().width;
            const maxRightWidth = containerRect.width - leftWidth - minMiddleWidth - 8;

            if (newRightWidth >= minRightWidth && newRightWidth <= maxRightWidth) {
                this.rightPanel.style.flex = 'none';
                this.rightPanel.style.width = `${newRightWidth}px`;
            }
        }
    }

    stopResize() {
        if (!this.isResizing) return;

        this.isResizing = false;
        if (this.currentResizer) {
            this.currentResizer.classList.remove('resizing');
            this.currentResizer = null;
        }

        document.body.style.cursor = '';
        document.body.style.userSelect = '';
    }
}
