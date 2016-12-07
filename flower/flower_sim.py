"""Main FLOWER simulation logic"""

import logging
import statistics
import warnings
import time

from operator import itemgetter
from typing import List

import matplotlib.pyplot as plt
import numpy as np
import quantities as pq

from core.cluster import combine_clusters
from core.cluster import closest_nodes
from core import environment
from core import segment
from flower.cluster import FlowerVirtualCluster
from flower.cluster import FlowerCluster
from flower.cluster import FlowerHub
from flower.cluster import FlowerVirtualHub
from flower import flower_runner
from flower import grid
from flower.energy import FlowerEnergyModel

logging.basicConfig(level=logging.DEBUG)
warnings.filterwarnings('error')


def much_greater_than(lhs, rhs, r=0.2):
    if rhs / lhs < r:
        return True

    return False


class FlowerError(Exception):
    pass


class Flower(object):
    def __init__(self, locs):

        self.env = environment.Environment()
        self.segments = [segment.Segment(loc) for loc in locs]
        self.grid = grid.Grid(self.segments)
        self.cells = [self.grid.cells()]

        self.damaged = self.grid.center()
        self.energy_model = FlowerEnergyModel(self)

        self.virtual_clusters = list()  # type: List[FlowerVirtualCluster]
        self.clusters = list()  # type: List[FlowerCluster]

        self.mech_energy = 0
        self.comms_energy = 0

        # Create a virtual segment to represent the center of the damaged
        # area
        virtual_center_cell = self.damaged

        self.virtual_hub = FlowerVirtualHub()
        self.virtual_hub.add(virtual_center_cell)

        self.hub = FlowerHub()
        self.hub.add(virtual_center_cell)

        self.em_is_large = False
        self.ec_is_large = False

    def show_state(self):

        fig = plt.figure()
        ax = fig.add_subplot(111)

        # Show the location of all segments
        segment_points = [seg.location.nd for seg in self.segments]
        segment_points = np.array(segment_points)
        ax.plot(segment_points[:, 0], segment_points[:, 1], 'bo')

        # Show the location of all cells
        cell_points = [c.location.nd for c in self.cells]
        cell_points = np.array(cell_points)
        ax.plot(cell_points[:, 0], cell_points[:, 1], 'rx')

        # Draw lines between each cell the virtual clusters to illustrate the
        # virtual cluster formations.
        for clust in self.virtual_clusters:
            route = clust.tour
            cps = route.collection_points
            ax.plot(cps[:, 0], cps[:, 1], 'go')
            ax.plot(cps[route.vertices, 0], cps[route.vertices, 1],
                    'g--', lw=2)

        plt.show()

    def init_cells(self):

        for cell in self.grid.cells():
            # Calculate the cell's proximity as it's cell distance from
            # the center of the "damaged area."
            cell.proximity = grid.cell_distance(cell, self.damaged)

        # Calculate the number of one-hop segments within range of each cell
        for cell in self.grid.cells():
            segments = set()
            for nbr in cell.neighbors:
                segments = set.union(segments, nbr.segments)

            cell.signal_hop_count = len(segments)

        # Calculate the set cover over the segments
        segment_cover = set()
        cell_cover = set()

        cells = list(self.grid.cells())

        while segment_cover != set(self.segments):

            candidate = None
            for cell in cells:
                if cell.access == 0:
                    continue

                if not candidate:
                    candidate = cell

                if len(segment_cover) == 0:
                    break

                if cell == self.damaged:
                    continue

                pot_cell_union = len(segment_cover.union(cell.segments))
                pot_candidate_union = len(
                    segment_cover.union(candidate.segments))

                if pot_candidate_union < pot_cell_union:
                    candidate = cell
                    continue

                elif pot_candidate_union == pot_cell_union:

                    if candidate.access < cell.access:
                        candidate = cell
                        continue

                    if candidate.signal_hop_count < cell.signal_hop_count:
                        candidate = cell
                        continue

                    if candidate.proximity > cell.proximity:
                        candidate = cell
                        continue

            segment_cover.update(candidate.segments)
            cell_cover.add(candidate)

        # Initialized!!
        logging.info("Length of cover: %d", len(cell_cover))

        assert self.env.mdc_count < len(cell_cover)

        # For future lookups, set a reference from each segment to its cell
        for cell in cell_cover:
            for seg in cell.segments:
                seg.cell = cell

        self.cells = cell_cover

    @staticmethod
    def _polar_angle(point, origin):
        vector = point - origin
        angle = np.arctan2(vector[1], vector[0])
        return angle

    def _polar_sort(self, clusters):
        """

        :param clusters:
        :type clusters: list(FlowerCluster)
        :return:
        """

        points = [c.location.nd for c in clusters]
        origin = min(points, key=itemgetter(0, 1))

        polar_angles = [self._polar_angle(p, origin) for p in points]
        indexes = np.argsort(polar_angles)

        sorted_clusters = np.array(clusters)[indexes]
        return list(sorted_clusters)

    def create_virtual_clusters(self):

        virtual_clusters = list()
        for cell in self.cells:
            c = FlowerVirtualCluster(self.virtual_hub)
            c.add(cell)
            virtual_clusters.append(c)

        # Combine the clusters until we have MDC_COUNT - 1 non-central, virtual
        # clusters
        while len(virtual_clusters) >= self.env.mdc_count:
            logging.info("Current VCs: %r", virtual_clusters)
            virtual_clusters = combine_clusters(virtual_clusters,
                                                self.virtual_hub)

        # FLOWER has some dependencies on the order of cluster IDs, so we need
        # to sort and re-label each virtual cluster.
        sorted_clusters = self._polar_sort(virtual_clusters)
        for i, vc in enumerate(sorted_clusters):
            vc.cluster_id = i

        for vc in virtual_clusters:
            for cell in vc.cells:
                logging.info("%s is in %s", cell, vc)

        self.virtual_clusters = virtual_clusters

    def handle_large_em(self):

        for vc in self.virtual_clusters:
            c = FlowerCluster(self.hub)
            c.cluster_id = vc.cluster_id

            # closest_cell, _ = closest_nodes(vc, self.hub)
            # closest_cell.cluster_id = c.cluster_id

            for cl in vc.cells:
                c.add(cl)

            self.clusters.append(c)

    def handle_large_ec(self):

        # start off the same as Em >> Ec
        self.handle_large_em()

        # in rounds, start optimizing
        all_clusters = self.clusters + [self.hub]

        r = 0
        while True:

            if r > 100:
                raise FlowerError("Optimization got lost")

            stdev = np.std([self.energy_model.total_energy(c.cluster_id)
                            for c in all_clusters])

            c_most = max(all_clusters,
                         key=lambda x: self.energy_model.total_energy(
                             x.cluster_id))

            # get the neighbors of c_most
            neighbors = [c for c in all_clusters if
                         abs(c.cluster_id - c_most.cluster_id) == 1]

            # find the minimum energy neighbor
            neighbor = min(neighbors,
                           key=lambda x: self.total_cluster_energy(x))

            # find the cell in c_most nearest the neighbor
            c_out, _ = closest_nodes(c_most, neighbor)

            c_most.remove(c_out)
            neighbor.add(c_out)

            # emulate a do ... while loop
            stdev_new = np.std(
                [self.total_cluster_energy(c) for c in all_clusters])
            r += 1
            logging.info("Completed %d rounds of Ec >> Em", r)

            # if this round didn't reduce stdev, then revert the changes and
            # exit the loop
            if stdev_new >= stdev:
                neighbor.remove(c_out)
                c_most.add(c_out)
                break

    def greedy_expansion(self):

        # First round (initial cell setup and energy calculation)

        for vc in self.virtual_clusters:
            c = FlowerCluster(self.hub)
            c.cluster_id = vc.cluster_id

            closest_cell, _ = closest_nodes(vc, self.hub)
            c.add(closest_cell)
            self.clusters.append(c)

        # Rounds 2 through N
        r = 1
        while any(not c.completed for c in self.clusters):

            r += 1

            # Determine the minimum-cost cluster by first filtering out all
            # non-completed clusters. Then find the the cluster with the lowest
            # total cost.
            candidates = self.clusters + [self.hub]
            candidates = [c for c in candidates if not c.completed]
            c_least = min(candidates,
                          key=lambda x: self.total_cluster_energy(x))

            # In general, only consider cells that have not already been added
            # to a cluster. There is an exception to this when expanding the
            # hub cluster.
            cells = [c for c in self.cells if c.cluster_id == -1]

            # If there are no more cells to assign, then we mark this cluster
            # as "completed"
            if not cells:
                c_least.completed = True
                logging.info("All cells assigned. Marking %s as completed",
                             c_least)
                continue

            if c_least == self.hub:

                # This logic handles the case where the hub cluster is has the
                # fewest energy requirements. Either the cluster will be moved
                # (initialization) or it will be grown.
                #
                # If the hub cluster is still in its original location at the
                # center of the damaged area, we need to move it to an actual
                # cell. If the hub has already been moved, then we expand it by
                # finding the cell nearest to the center of the damaged area,
                # and that itself hasn't already been added to the hub cluster.

                if c_least.cells == [self.damaged]:
                    # Find the nearest cell to the center of the damaged area
                    # and move the hub to it. This is equivalent to finding the
                    # cell with the lowest proximity.
                    best_cell = min(cells, key=lambda x: x.proximity)

                    # As the hub only currently has the virtual center cell in
                    # it, we can just "move" the hub to the nearest real cell
                    # by replacing the virtual cell with it.
                    self.hub.nodes = [best_cell]
                    best_cell.cluster = self.hub

                    # Just for proper bookkeeping, reset the virtual cell's ID
                    # to NOT_CLUSTERED
                    self.damaged.cluster_id = -1
                    logging.info("ROUND %d: Moved %s to %s", r, self.hub,
                                 best_cell)

                else:
                    # Find the set of cells that are not already in the hub
                    # cluster
                    available_cells = list(
                        set(self.cells) - set(self.hub.cells))

                    # Out of those cells, find the one that is closest to the
                    # damaged area
                    best_cell, _ = closest_nodes(available_cells,
                                                 [self.hub.recent])

                    # Add that cell to the hub cluster
                    self.hub.add(best_cell)

                    logging.info("ROUND %d: Added %s to %s", r, best_cell,
                                 self.hub)

                # Set the cluster ID for the new cell, mark it as the most
                # recent cell for the hub cluster and update the anchors for
                # all other clusters.
                best_cell.cluster_id = self.hub.cluster_id
                c_least.recent = best_cell

            else:

                # In this case, the cluster with the lowest energy requirements
                # is one of the non-hub clusters.

                best_cell = None

                # Find the VC that corresponds to the current cluster
                vci = next(vc for vc in self.virtual_clusters if
                           vc.cluster_id == c_least.cluster_id)

                # Get a list of the cells that have not yet been added to a
                # cluster
                candidates = [c for c in vci.cells if c.cluster_id == -1]

                if candidates:

                    # Find the cell that is closest to the cluster's recent
                    # cell
                    best_cell, _ = closest_nodes(candidates, [c_least.recent])

                else:
                    for i in range(1, max(self.grid.cols, self.grid.rows) + 1):
                        recent = c_least.recent
                        nbrs = self.grid.cell_neighbors(recent.row, recent.col,
                                                        radius=i)

                        for nbr in nbrs:
                            # filter out cells that are not part of a virtual
                            # cluster
                            if nbr.virtual_cluster_id == -1:
                                continue

                            # filter out cells that are not in neighboring VCs
                            dist = abs(nbr.virtual_cluster_id - vci.cluster_id)
                            if dist != 1:
                                continue

                            # if the cell we find is already clustered, we are
                            # done working on this cluster
                            if nbr.cluster_id != -1:
                                c_least.completed = True
                                break

                            best_cell = nbr
                            break

                        if best_cell or c_least.completed:
                            break

                if best_cell:
                    logging.info("ROUND %d: Added %s to %s", r, best_cell,
                                 c_least)
                    c_least.add(best_cell)

                else:
                    c_least.completed = True
                    logging.info(
                        "ROUND %d: No best cell found. Marking %s completed",
                        r, c_least)

    def total_cluster_energy(self, c):
        energy = self.energy_model.total_energy(c.cluster_id)
        return energy

    def optimization(self):

        all_clusters = self.clusters + [self.hub]

        r = 0
        while True:

            stdev = np.std(
                [self.total_cluster_energy(c) for c in all_clusters])
            c_least = min(all_clusters,
                          key=lambda x: self.total_cluster_energy(x))
            c_most = max(all_clusters,
                         key=lambda x: self.total_cluster_energy(x))

            if r > 100:
                raise FlowerError("Optimization got lost")

            if self.hub == c_least:
                _, c_in = closest_nodes([c_most.anchor], c_most)

                c_most.remove(c_in)
                self.hub.add(c_in)

                # emulate a do ... while loop
                stdev_new = np.std(
                    [self.total_cluster_energy(c) for c in all_clusters])
                r += 1
                logging.info("Completed %d rounds of 2b", r)

                # if this round didn't reduce stdev, then revert the changes
                # and exit the loop
                if stdev_new >= stdev:
                    self.hub.remove(c_in)
                    c_most.add(c_in)
                    break

            elif self.hub == c_most:
                # shrink c_most
                c_out, _ = closest_nodes(self.hub, [c_least.anchor])

                self.hub.remove(c_out)
                c_least.add(c_out)

                # emulate a do ... while loop
                stdev_new = np.std(
                    [self.total_cluster_energy(c) for c in all_clusters])
                r += 1
                logging.info("Completed %d rounds of 2b", r)

                # if this round didn't reduce stdev, then revert the changes
                # and exit the loop
                if stdev_new >= stdev:
                    c_least.remove(c_out)
                    self.hub.add(c_out)
                    break

            else:
                # grow c_least
                c_out, _ = closest_nodes(self.hub, [c_least.anchor])
                self.hub.remove(c_out)
                c_least.add(c_out)

                # shrink c_most
                _, c_in = closest_nodes([c_most.anchor], c_most)
                c_most.remove(c_in)
                self.hub.add(c_in)

                # emulate a do ... while loop
                stdev_new = np.std(
                    [self.total_cluster_energy(c) for c in all_clusters])
                r += 1
                logging.info("Completed %d rounds of 2b", r)

                # if this round didn't reduce stdev, then revert the changes
                # and exit the loop
                if stdev_new >= stdev:
                    c_least.remove(c_out)
                    self.hub.add(c_out)

                    self.hub.remove(c_in)
                    c_most.add(c_in)
                    break

    def compute_paths(self):
        self.init_cells()
        self.create_virtual_clusters()

        # Check for special cases (Em >> Ec or Ec >> Em)
        self.mech_energy = self.energy_model.total_sim_movement_energy(True)
        self.comms_energy = self.energy_model.total_sim_comms_energy(True)

        logging.info("Initial mech energy: %s", self.mech_energy)
        logging.info("Initial comms energy: %s", self.comms_energy)

        if much_greater_than(self.mech_energy, self.comms_energy):
            logging.info("Handling special case Em >> Ec")
            self.em_is_large = True
            self.handle_large_em()

        elif much_greater_than(self.comms_energy, self.mech_energy):
            logging.info("Handling special case Ec >> Em")
            self.ec_is_large = True
            self.handle_large_ec()

        else:
            self.greedy_expansion()
            self.optimization()

            # Check for special cases (Em >> Ec or Ec >> Em)
            # self.mech_energy = self.energy_model.total_sim_movement_energy(False)
            # self.comms_energy = self.energy_model.total_sim_comms_energy(False)
            #
            # if much_greater_than(self.mech_energy, self.comms_energy):
            #     logging.info("Need to conduct tour sharing (Em >> Ec)")
            #     self.em_is_large = True
            #
            # elif much_greater_than(self.comms_energy, self.mech_energy):
            #     logging.info("Need to conduct tour sharing (Ec >> Em)")
            #     self.ec_is_large = True

        # self.show_state()

        return self

    def run(self):
        sim = self.compute_paths()
        # return flower_runner.run_sim(sim)


def main():
    env = environment.Environment()
    # env.grid_height = 20000. * pq.meter
    # env.grid_width = 20000. * pq.meter
    seed = int(time.time())
    # seed = 1480824633
    logging.debug("Random seed is %s", seed)
    np.random.seed(seed)
    locs = np.random.rand(env.segment_count, 2) * env.grid_height
    sim = Flower(locs)
    sim.run()


if __name__ == '__main__':
    main()