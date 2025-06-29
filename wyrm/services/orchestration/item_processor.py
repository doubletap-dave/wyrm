"""Item processing for Wyrm orchestration.

Handles the logic for processing items from a sidebar structure,
including filtering, processing mode selection, and execution.
"""

from typing import Dict, List, Optional

import structlog

from wyrm.models.scrape import SidebarStructure
from .item_handler import ItemHandler
from .performance_analyzer import PerformanceAnalyzer
from .parallel_coordinator import ParallelCoordinator


class ItemProcessor:
    """Processes items from the sidebar structure for content extraction."""

    def __init__(self, orchestrator):
        """Initialize item processor with orchestrator reference.

        Args:
            orchestrator: Reference to the main Orchestrator instance.
        """
        self.orchestrator = orchestrator
        self.logger = structlog.get_logger(__name__)
        self.item_handler = ItemHandler()
        self.performance_analyzer = PerformanceAnalyzer()
        self.parallel_coordinator = ParallelCoordinator(orchestrator)

    async def process_items_from_structure(
        self,
        sidebar_structure: SidebarStructure,
        config_values: Dict,
        force: bool,
        test_item_id: Optional[str],
        max_items: Optional[int],
        resume_info: bool,
        from_cache: bool,
    ) -> None:
        """Process items from the sidebar structure.

        Filters items based on provided criteria and processes them for content
        extraction. Handles test mode, item limits, and force overrides.

        Args:
            sidebar_structure: Parsed sidebar structure containing all items.
            config_values: Configuration values for processing.
            force: Whether to overwrite existing files.
            test_item_id: DEPRECATED. Specific item ID to process.
            max_items: Maximum number of items to process.
            resume_info: Whether this is resume info mode (affects processing).
            from_cache: Whether this structure was loaded from cache.

        Returns:
            None
        """
        valid_items = self.orchestrator.parsing_service._get_valid_items(
            sidebar_structure
        )

        # Filter items for processing
        items_to_process = self.orchestrator.parsing_service.filter_items_for_processing(
            valid_items, max_items, test_item_id
        )

        # Log whether structure came from cache or was parsed live
        if from_cache:
            self.logger.info(
                "Processing items from cached structure",
                count=len(items_to_process),
                source="cache"
            )
        else:
            self.logger.info(
                "Processing items from live-parsed structure",
                count=len(items_to_process),
                source="live_parsing"
            )

        self.logger.info("Processing items", count=len(items_to_process))

        # Initialize NavigationService if not already initialized
        if not self.orchestrator.navigation_service.get_driver():
            self.logger.info("Initializing NavigationService for item processing")
            await self.orchestrator.navigation_service.initialize_driver(
                self.orchestrator._config
            )

            self.logger.info("NavigationService initialized successfully")

            # Navigate and expand menus if using cached data
            self.logger.info(
                "Navigating to site and expanding menus for cached data processing..."
            )
            await self.orchestrator.navigation_service.navigate_and_wait(
                self.orchestrator._config, config_values
            )
            await self.orchestrator.navigation_service.expand_all_menus_comprehensive()

        # Process items using hybrid mode
        await self._process_items_hybrid_mode(
            items_to_process, config_values
        )

    async def _process_items_hybrid_mode(
        self,
        items_to_process: List[Dict],
        config_values: Dict,
    ) -> None:
        """Choose between parallel and sequential processing modes.

        This method implements the hybrid approach:
        1. Check if parallel processing is enabled and feasible
        2. Estimate performance benefits
        3. Choose the optimal processing mode
        4. Provide graceful fallback if parallel processing fails

        Args:
            items_to_process: List of sidebar items to process
            config_values: Configuration values including concurrency settings

        Returns:
            None
        """
        # Check if we should use sequential processing
        if self.performance_analyzer.should_use_sequential_processing(items_to_process, config_values):
            await self._process_items_with_progress(items_to_process, config_values)
            return

        # Try parallel processing with fallback
        parallel_success = await self.parallel_coordinator.try_parallel_processing(items_to_process, config_values)
        if not parallel_success:
            # Fallback to sequential processing
            await self._process_items_with_progress(items_to_process, config_values)

    def _convert_to_sidebar_items(self, items_to_process: List[Dict]) -> List:
        """Convert dict items to SidebarItem objects for parallel processing."""
        return self.item_handler.convert_to_sidebar_items(items_to_process)

    async def _process_items_with_progress(
        self,
        items_to_process: List[Dict],
        config_values: Dict,
    ) -> None:
        """Process items with progress tracking and reporting.

        Iterates through items and processes each one individually while
        maintaining progress reporting and error handling for individual failures.

        Args:
            items_to_process: List of sidebar items to process.
            config_values: Configuration values for processing.

        Returns:
            None
        """
        progress = self.orchestrator.progress_service.create_progress_display()

        with self.orchestrator.progress_service._suppress_console_logging():
            with progress:
                task_id = progress.add_task(
                    "Processing items...", total=len(items_to_process)
                )

                for item in items_to_process:
                    try:
                        await self._process_single_item(
                            item, config_values, progress, task_id
                        )
                    except Exception as e:
                        # Log to file only during progress display
                        # Handle both SidebarItem models and dict items for backward compatibility
                        if hasattr(item, 'id'):
                            item_id = item.id
                        else:
                            item_id = item.get('id', 'unknown')
                        
                        self.orchestrator.logger.exception(
                            "Failed to process item",
                            item_id=item_id,
                            error=str(e)
                        )
                        continue

    async def _process_single_item(
        self,
        item: Dict,
        config_values: Dict,
        progress=None,
        task_id=None,
    ) -> None:
        """Process a single sidebar item for content extraction.

        Handles individual item processing including navigation, content extraction,
        and file saving with comprehensive error handling and progress reporting.

        Args:
            item: Dictionary containing item information (id, text, menu, etc.).
            config_values: Configuration values for processing.
            progress: Optional progress display instance for UI updates.
            task_id: Optional task ID for progress tracking.

        Returns:
            None
        """
        # Handle both SidebarItem models and dict items for backward compatibility
        if hasattr(item, 'id'):
            item_id = item.id
            item_text = item.text
        else:
            item_id = item.get('id')
            item_text = item.get('text', 'Unknown')
        
        # Check if file already exists and skip if not forcing
        if not config_values.get('force', False):
            existing_file = self.orchestrator.storage_service.get_output_path(
                item, config_values['base_output_dir']
            )
            if existing_file.exists():
                self.logger.debug(f"Skipping existing file: {existing_file}")
                if progress and task_id is not None:
                    progress.update(task_id, advance=1, description=f"Skipped: {item_text}")
                return

        # Process the item
        try:
            # Navigate to item and extract content
            success = await self.orchestrator.navigation_service.navigate_to_item(
                item, config_values
            )
            
            if not success:
                self.logger.warning(f"Failed to navigate to item: {item_text}")
                return

            # Extract and save content
            await self.orchestrator.storage_service.save_content_for_item(
                item, self.orchestrator.navigation_service.get_driver(), config_values
            )
            
            if progress and task_id is not None:
                progress.update(task_id, advance=1, description=f"Processed: {item_text}")
                
        except Exception as e:
            self.logger.error(
                f"Error processing item {item_text}: {str(e)}",
                item_id=item_id,
                error=str(e)
            )
            if progress and task_id is not None:
                progress.update(task_id, advance=1, description=f"Failed: {item_text}")
