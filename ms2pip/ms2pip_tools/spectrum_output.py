"""
Write spectrum files from MS2PIP predictions.
"""


# Native libraries
from time import localtime, strftime
from ast import literal_eval
from operator import itemgetter
from io import StringIO
import os

# Third party libraries
import pandas as pd
from pyteomics import mass

PROTON_MASS = 1.007825032070059


class SpectrumOutput:
    """
    Write MS2PIP predictions to various output formats.

    Parameters
    ----------
    all_preds: pd.DataFrame
        MS2PIP predictions
    peprec: pd.DataFrame
        PEPREC with peptide information
    params: dict
        MS2PIP parameters
    output_filename: str, optional
        path and name for output files, will be suffexed with `_predictions` and the
        relevant file extension (default: ms2pip_predictions)
    write_mode: str, optional
        write mode to use: "wt+" to append to start a new file, "at" to append to an
        existing file (default: "wt+")
    return_stringbuffer: bool, optional
        If True, files are written to a StringIO object, which the write function
        returns. If False, files are written to a file on disk.
    is_log_space: bool, optional
        Set to true if predicted intensities in `all_preds` are in log-space. In that
        case, intensities will first be transformed to "normal"-space.
        
    Methods
    -------
    write_msp()
        Write predictions to MSP file
    write_mgf()
        Write predictions to MGF file
    write_bibliospec()
        Write predictions to Bibliospec SSL/MS2 files (also for Skyline)
    write_spectronaut()
        Write predictions to Spectronaut CSV file
    
    Example
    -------
    >>> so = ms2pip.spectrum_tools.spectrum_output.SpectrumOutput(
            all_preds,
            peprec,
            params
        )
    >>> so.write_msp()
    >>> so.write_spectronaut()

    """

    def __init__(
        self,
        all_preds,
        peprec,
        params,
        output_filename="ms2pip_predictions",
        write_mode="wt+",
        return_stringbuffer=False,
        is_log_space=True,
    ):
        self.all_preds = all_preds
        self.peprec = peprec
        self.params = params
        self.output_filename = output_filename
        self.write_mode = write_mode
        self.return_stringbuffer = return_stringbuffer
        self.is_log_space = is_log_space

        self.peprec_dict = None
        self.preds_dict = None
        self.normalization = None
        self.mass_shifts = None
        self.ssl_modification_mapping = None

        self.has_rt = "rt" in self.peprec.columns
        self.has_protein_list = "protein_list" in self.peprec.columns

        self._generate_mass_shifts()

        if self.write_mode not in ["wt+", "wt", "at", "w", "a"]:
            raise ValueError("Invalid write_mode: ", self.write_mode)

    def _generate_peprec_dict(self, rt_to_seconds=True):
        """
        Create easy to access dict from all_preds and peprec dataframes
        """
        peprec_tmp = self.peprec.copy()

        if self.has_rt and rt_to_seconds:
            peprec_tmp["rt"] = peprec_tmp["rt"] * 60

        peprec_tmp.index = peprec_tmp["spec_id"]
        peprec_tmp.drop("spec_id", axis=1, inplace=True)

        self.peprec_dict = peprec_tmp.to_dict(orient="index")

    def _generate_preds_dict(self):
        """
        Create easy to access dict from peprec dataframes
        """
        self.preds_dict = {}
        preds_list = self.all_preds[
            ["spec_id", "charge", "ion", "ionnumber", "mz", "prediction"]
        ].values.tolist()

        for row in preds_list:
            spec_id = row[0]
            if spec_id in self.preds_dict.keys():
                if row[2] in self.preds_dict[spec_id]["peaks"]:
                    self.preds_dict[spec_id]["peaks"][row[2]].append(tuple(row[3:]))
                else:
                    self.preds_dict[spec_id]["peaks"][row[2]] = [tuple(row[3:])]
            else:
                self.preds_dict[spec_id] = {
                    "charge": row[1],
                    "peaks": {row[2]: [tuple(row[3:])]},
                }

    def _generate_mass_shifts(self):
        """
        Make modification name -> mass shift mapping.
        """
        self.mass_shifts = {
            ptm.split(",")[0]: float(ptm.split(",")[1]) for ptm in self.params["ptm"]
        }

    def _get_precursor_mz(self, peptide, modifications, charge):
        """
        Calculate precursor mass and mz for given peptide and modification list,
        using Pyteomics.

        Note: This method does not use the build-in Pyteomics modification handling, as
        that would require a known atomic composition of the modification.

        Parameters
        ----------
        peptide: str
            stripped peptide sequence

        modifications: str
            MS2PIP-style formatted modifications list (e.g. `0|Acetyl|2|Oxidation`)

        charge: int
            precursor charge

        mass_shifts: dict(str, float)
            dictionary with `modification_name -> mass_shift` pairs

        Returns
        -------
        prec_mass, prec_mz: tuple(float, float)
        """

        charge = int(charge)
        unmodified_mass = mass.fast_mass(peptide)
        mods_massses = sum(
            [self.mass_shifts[mod] for mod in modifications.split("|")[1::2]]
        )
        prec_mass = unmodified_mass + mods_massses
        prec_mz = (prec_mass + charge * PROTON_MASS) / charge
        return prec_mass, prec_mz

    def _normalize_spectra(self, method="basepeak_10000"):
        """
        Normalize spectra
        """
        if self.is_log_space:
            self.all_preds["prediction"] = (
                (2 ** self.all_preds["prediction"]) - 0.001
            ).clip(lower=0)
            self.is_log_space = False

        if method == "basepeak_10000":
            if self.normalization == "basepeak_10000":
                pass
            elif self.normalization == "basepeak_1":
                self.all_preds["prediction"] *= 10000
                self.all_preds["prediction"] = self.all_preds["prediction"]
            else:
                self.all_preds["prediction"] = self.all_preds.groupby(["spec_id"])[
                    "prediction"
                ].apply(lambda x: (x / x.max()) * 10000)
                self.all_preds["prediction"] = self.all_preds["prediction"]
            self.normalization = "basepeak_10000"

        elif method == "basepeak_1":
            if self.normalization == "basepeak_1":
                pass
            elif self.normalization == "basepeak_10000":
                self.all_preds["prediction"] /= 10000
            else:
                self.all_preds["prediction"] = self.all_preds.groupby(["spec_id"])[
                    "prediction"
                ].apply(lambda x: (x / x.max()))
            self.normalization = "basepeak_1"

        elif method == "tic" and not self.normalization == "tic":
            self.all_preds["prediction"] = self.all_preds.groupby(["spec_id"])[
                "prediction"
            ].apply(lambda x: x / x.sum())
            self.normalization = "tic"

    def _get_peak_string(
        self,
        peak_dict,
        sep="\t",
        include_zero=False,
        include_annotations=True,
        intensity_type=float,
    ):
        """
        Get MGF/MSP-like peaklist string
        """
        all_peaks = []
        for ion_type, peaks in peak_dict.items():
            for peak in peaks:
                if not include_zero and peak[2] == 0:
                    continue
                if include_annotations:
                    all_peaks.append(
                        (
                            peak[1],
                            f'{peak[1]:.6f}{sep}{intensity_type(peak[2])}{sep}"{ion_type.lower()}{peak[0]}"',
                        )
                    )
                else:
                    all_peaks.append((peak[1], f"{peak[1]:.6f}{sep}{peak[2]}"))

        all_peaks = sorted(all_peaks, key=itemgetter(0))
        peak_string = "\n".join([peak[1] for peak in all_peaks])

        return peak_string

    def _get_msp_modifications(self, sequence, modifications):
        """
        Format modifications in MSP-style, e.g. "1/0,E,Glu->pyro-Glu"
        """

        if isinstance(modifications, str):
            if modifications == "-":
                msp_modifications = "0"
            else:
                mods = modifications.split("|")
                mods = [(int(mods[i]), mods[i + 1]) for i in range(0, len(mods), 2)]
                mods = [(x, y) if x == 0 else (x - 1, y) for (x, y) in mods]
                mods = [(str(x), sequence[x], y) for (x, y) in mods]
                msp_modifications = "/".join([",".join(list(x)) for x in mods])
                msp_modifications = f"{len(mods)}/{msp_modifications}"
        else:
            msp_modifications = "0"

        return msp_modifications

    def _parse_protein_string(self, protein_list):
        """
        Parse protein string from list, list string literal, or string.
        """
        if isinstance(protein_list, list):
            protein_string = "/".join(protein_list)
        elif isinstance(protein_list, str):
            try:
                protein_string = "/".join(literal_eval(protein_list))
            except ValueError:
                protein_string = protein_list
        else:
            protein_string = ""
        return protein_string

    def _write_msp_core(self, file_object):
        """
        Construct MSP string and write to file_object.
        """

        for spec_id in sorted(self.peprec_dict.keys()):
            seq = self.peprec_dict[spec_id]["peptide"]
            mods = self.peprec_dict[spec_id]["modifications"]
            charge = self.peprec_dict[spec_id]["charge"]
            prec_mass, prec_mz = self._get_precursor_mz(seq, mods, charge)
            msp_modifications = self._get_msp_modifications(seq, mods)
            num_peaks = sum(
                [
                    len(peaklist)
                    for _, peaklist in self.preds_dict[spec_id]["peaks"].items()
                ]
            )

            comment_line = f" Mods={msp_modifications} Parent={prec_mz}"

            if self.has_protein_list:
                protein_list = self.peprec_dict[spec_id]["protein_list"]
                protein_string = self._parse_protein_string(protein_list)
                comment_line += f' Protein="{protein_string}"'

            if self.has_rt:
                rt = self.peprec_dict[spec_id]["rt"]
                comment_line += f" RTINSECONDS={rt}"

            comment_line += f' MS2PIP_ID="{spec_id}"'

            out = [
                f"Name: {seq}/{charge}",
                f"MW: {prec_mass}",
                f"Comment:{comment_line}",
                f"Num peaks: {num_peaks}",
                self._get_peak_string(
                    self.preds_dict[spec_id]["peaks"],
                    sep="\t",
                    include_annotations=True,
                    intensity_type=int,
                ),
            ]

            file_object.writelines([line + "\n" for line in out] + ["\n"])

    def write_msp(self):
        """
        Write MS2PIP predictions to MSP spectral library file.
        """

        # Normalize if necessary and make dicts
        if not self.normalization == "basepeak_10000":
            self._normalize_spectra(method="basepeak_10000")
            self._generate_preds_dict()
        elif not self.preds_dict:
            self._generate_preds_dict()
        if not self.peprec_dict:
            self._generate_peprec_dict()

        # Write to file or stringbuffer
        if self.return_stringbuffer:
            file_object = StringIO()
            self._write_msp_core(file_object)
            return file_object
        else:
            with open(
                "{}_predictions.msp".format(self.output_filename), self.write_mode
            ) as file_object:
                self._write_msp_core(file_object)

    def _write_mgf_core(self, file_object):
        """
        Construct MGF string and write to file_object
        """
        for spec_id in sorted(self.peprec_dict.keys()):
            seq = self.peprec_dict[spec_id]["peptide"]
            mods = self.peprec_dict[spec_id]["modifications"]
            charge = self.peprec_dict[spec_id]["charge"]
            prec_mass, prec_mz = self._get_precursor_mz(seq, mods, charge)
            msp_modifications = self._get_msp_modifications(seq, mods)

            if self.has_protein_list:
                protein_list = self.peprec_dict[spec_id]["protein_list"]
                protein_string = self._parse_protein_string(protein_list)
            else:
                protein_string = ""

            out = [
                "BEGIN IONS",
                f"TITLE={spec_id} {seq}/{charge} {msp_modifications} {protein_string}",
                f"PEPMASS={prec_mz}",
                f"CHARGE={charge}+",
            ]

            if self.has_rt:
                rt = self.peprec_dict[spec_id]["rt"]
                out.append(f"RTINSECONDS={rt}")

            out.append(
                self._get_peak_string(
                    self.preds_dict[spec_id]["peaks"],
                    sep=" ",
                    include_annotations=False,
                )
            )
            out.append("END IONS\n")
            file_object.writelines([line + "\n" for line in out])

    def write_mgf(self):
        """
        Write MS2PIP predictions to MGF spectrum file.
        """

        # Normalize if necessary and make dicts
        if not self.normalization == "basepeak_10000":
            self._normalize_spectra(method="basepeak_10000")
            self._generate_preds_dict()
        elif not self.preds_dict:
            self._generate_preds_dict()
        if not self.peprec_dict:
            self._generate_peprec_dict()

        if self.return_stringbuffer:
            file_object = StringIO()
            self._write_mgf_core(file_object)
            return file_object
        else:
            with open(
                "{}_predictions.mgf".format(self.output_filename), self.write_mode
            ) as file_object:
                self._write_mgf_core(file_object)

    def _get_last_ssl_scannr(self):
        """
        Return scan number of last line in a Bibliospec SSL file.
        """
        ssl_filename = "{}_predictions.ssl".format(self.output_filename)
        with open(ssl_filename, "rt") as ssl:
            for line in ssl:
                last_line = line
            last_scannr = int(last_line.split("\t")[1])
        return last_scannr

    def _generate_ssl_modification_mapping(self):
        """
        Make modification name -> ssl modification name mapping.
        """
        self.ssl_modification_mapping = {
            ptm.split(",")[0]: "{:+.1f}".format(round(float(ptm.split(",")[1]), 1))
            for ptm in self.params["ptm"]
        }

    def _get_ssl_modified_sequence(self, sequence, modifications):
        """
        Build BiblioSpec SSL modified sequence string.
        """
        pep = list(sequence)

        for loc, name in zip(
            modifications.split("|")[::2], modifications.split("|")[1::2]
        ):
            # C-term mod
            if loc == "-1":
                pep[-1] = pep[-1] + "[{}]".format(self.ssl_modification_mapping[name])
            # N-term mod
            elif loc == "0":
                pep[0] = pep[0] + "[{}]".format(self.ssl_modification_mapping[name])
            # Normal mod
            else:
                pep[int(loc) - 1] = pep[int(loc) - 1] + "[{}]".format(
                    self.ssl_modification_mapping[name]
                )
        return "".join(pep)

    def _write_bibliospec_core(self, file_obj_ssl, file_obj_ms2, start_scannr=0):
        """
        Construct Bibliospec SSL/MS2 strings and write to file_objects.
        """

        for i, spec_id in enumerate(sorted(self.preds_dict.keys())):
            scannr = i + start_scannr
            seq = self.peprec_dict[spec_id]["peptide"]
            mods = self.peprec_dict[spec_id]["modifications"]
            charge = self.peprec_dict[spec_id]["charge"]
            prec_mass, prec_mz = self._get_precursor_mz(seq, mods, charge)
            ms2_filename = os.path.basename(self.output_filename) + "_predictions.ms2"

            peaks = self._get_peak_string(
                self.preds_dict[spec_id]["peaks"], sep="\t", include_annotations=False,
            )

            if isinstance(mods, str):
                if mods != "-" and mods != "":
                    mod_seq = self._get_ssl_modified_sequence(seq, mods)
                else:
                    mod_seq = seq
            else:
                mod_seq = seq

            rt = self.peprec_dict[spec_id]["rt"] if self.has_rt else ""

            file_obj_ssl.write(
                "\t".join(
                    [ms2_filename, str(scannr), str(charge), mod_seq, "", "", str(rt)]
                )
                + "\n"
            )
            file_obj_ms2.write(
                "\n".join(
                    [
                        f"S\t{scannr}\t{prec_mz}",
                        f"Z\t{charge}\t{prec_mass}",
                        f"D\tseq\t{seq}",
                        f"D\tmodified seq\t{mod_seq}",
                        peaks,
                    ]
                )
                + "\n"
            )

    def write_bibliospec(self):
        """
        Write MS2PIP predictions to BiblioSpec SSL and MS2 spectral library files
        (For example for use in Skyline).
        """

        if not self.ssl_modification_mapping:
            self._generate_ssl_modification_mapping()

        # Normalize if necessary and make dicts
        if not self.normalization == "basepeak_10000":
            self._normalize_spectra(method="basepeak_10000")
            self._generate_preds_dict()
        elif not self.preds_dict:
            self._generate_preds_dict()
        if not self.peprec_dict:
            self._generate_peprec_dict()

        if self.return_stringbuffer:
            file_obj_ssl = StringIO()
            file_obj_ms2 = StringIO()
        else:
            file_obj_ssl = open(
                "{}_predictions.ssl".format(self.output_filename), self.write_mode
            )
            file_obj_ms2 = open(
                "{}_predictions.ms2".format(self.output_filename), self.write_mode
            )

        # If a new file is written, write headers
        if "w" in self.write_mode:
            start_scannr = 0
            ssl_header = [
                "file",
                "scan",
                "charge",
                "sequence",
                "score-type",
                "score",
                "retention-time",
                "\n",
            ]
            file_obj_ssl.write("\t".join(ssl_header))
            file_obj_ms2.write(
                "H\tCreationDate\t{}\n".format(
                    strftime("%Y-%m-%d %H:%M:%S", localtime())
                )
            )
            file_obj_ms2.write("H\tExtractor\tMS2PIP predictions\n")
        else:
            # Get last scan number of ssl file, to continue indexing from there
            # because Bibliospec speclib scan numbers can only be integers
            start_scannr = self._get_last_ssl_scannr() + 1

        self._write_bibliospec_core(
            file_obj_ssl, file_obj_ms2, start_scannr=start_scannr
        )

        if self.return_stringbuffer:
            return file_obj_ssl, file_obj_ms2

    def _write_spectronaut_core(self, file_obj):
        """
        Construct spectronaut DataFrame and write to file_object.
        """
        if "w" in self.write_mode:
            header = True
        elif "a" in self.write_mode:
            header = False
        else:
            raise ValueError(self.write_mode)

        spectronaut_peprec = self.peprec.copy()

        # ModifiedPeptide and PrecursorMz columns
        spectronaut_peprec["ModifiedPeptide"] = spectronaut_peprec.apply(
            lambda row: self._get_ssl_modified_sequence(
                row["peptide"], row["modifications"]
            ),
            axis=1,
        )
        spectronaut_peprec["PrecursorMz"] = spectronaut_peprec.apply(
            lambda row: self._get_precursor_mz(
                row["peptide"], row["modifications"], row["charge"]
            )[1],
            axis=1,
        )
        spectronaut_peprec["ModifiedPeptide"] = (
            "_" + spectronaut_peprec["ModifiedPeptide"] + "_"
        )

        # Additional columns
        spectronaut_peprec["FragmentLossType"] = "noloss"

        # Retention time
        if "rt" in spectronaut_peprec.columns:
            rt_cols = ["iRT"]
            spectronaut_peprec["iRT"] = spectronaut_peprec["rt"]
        else:
            rt_cols = []

        # ProteinId
        if self.has_protein_list:
            spectronaut_peprec["ProteinId"] = spectronaut_peprec["protein_list"].apply(
                self._parse_protein_string
            )
        else:
            spectronaut_peprec["ProteinId"] = spectronaut_peprec["spec_id"]

        # Rename columns and merge with predictions
        spectronaut_peprec = spectronaut_peprec.rename(
            columns={"charge": "PrecursorCharge", "peptide": "StrippedPeptide"}
        )
        peptide_cols = (
            [
                "ModifiedPeptide",
                "StrippedPeptide",
                "PrecursorCharge",
                "PrecursorMz",
                "ProteinId",
            ]
            + rt_cols
            + ["FragmentLossType"]
        )
        spectronaut_df = spectronaut_peprec[peptide_cols + ["spec_id"]]
        spectronaut_df = self.all_preds.merge(spectronaut_df, on="spec_id")

        # Fragment columns
        spectronaut_df["FragmentCharge"] = (
            spectronaut_df["ion"].str.contains("2").map({True: 2, False: 1})
        )
        spectronaut_df["FragmentType"] = spectronaut_df["ion"].str[0].str.lower()

        # Rename and sort columns
        spectronaut_df = spectronaut_df.rename(
            columns={
                "mz": "FragmentMz",
                "prediction": "RelativeIntensity",
                "ionnumber": "FragmentNumber",
            }
        )
        fragment_cols = [
            "FragmentCharge",
            "FragmentMz",
            "RelativeIntensity",
            "FragmentType",
            "FragmentNumber",
        ]
        spectronaut_df = spectronaut_df[peptide_cols + fragment_cols]

        spectronaut_df.to_csv(file_obj, index=False, header=header)

    def write_spectronaut(self):
        """
        Write to Spectronaut library import format.

        Reference: https://biognosys.com/media.ashx/spectronautmanual.pdf
        """

        if not self.ssl_modification_mapping:
            self._generate_ssl_modification_mapping()

        # Normalize if necessary
        if not self.normalization == "tic":
            self._normalize_spectra(method="tic")

        if self.return_stringbuffer:
            file_obj = StringIO()
            self._write_spectronaut_core(file_obj)
            return file_obj
        else:
            f_name = "{}_predictions_spectronaut.csv".format(self.output_filename)
            with open(f_name, self.write_mode) as file_obj:
                self._write_spectronaut_core(file_obj)
